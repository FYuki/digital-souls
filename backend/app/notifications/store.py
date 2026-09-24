"""通知・設定・判定位置を原子的に保存するmetadata-only SQLite。"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Literal

from app.external_mcp.models import Json, digest, encode
from .contracts import Decision, Limits, Registration, failure


class NotificationStore:
    def __init__(self, path: Path, limits: Limits = Limits(), *, clock: Callable[[], float] = time.time) -> None:
        if path.is_symlink():
            raise failure("unsafe_notification_store")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        path.chmod(0o600)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.lock = RLock()
        self.limits, self.clock = limits, clock
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS notification_registrations (
                id TEXT PRIMARY KEY, signature TEXT NOT NULL, identity TEXT,
                binding_signature TEXT, stream TEXT, position INTEGER NOT NULL DEFAULT 0,
                gap_count INTEGER NOT NULL DEFAULT 0, last_gap TEXT,
                last_decision TEXT, observed TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY, registration TEXT NOT NULL,
                user_id TEXT NOT NULL, source_id TEXT NOT NULL, character_id TEXT NOT NULL,
                event_type TEXT NOT NULL, event_key TEXT NOT NULL, metadata TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'unread' CHECK(state IN ('unread','read','hidden')),
                version INTEGER NOT NULL DEFAULT 1,
                UNIQUE(registration,user_id,event_key),
                FOREIGN KEY(registration) REFERENCES notification_registrations(id)
            );
            CREATE INDEX IF NOT EXISTS notification_timeline ON notifications(user_id,created DESC,id);
            CREATE INDEX IF NOT EXISTS notification_expiry ON notifications(expires);
            CREATE TABLE IF NOT EXISTS notification_preferences (
                user_id TEXT NOT NULL, source_id TEXT NOT NULL, event_type TEXT NOT NULL,
                enabled INTEGER NOT NULL, changed REAL NOT NULL,
                PRIMARY KEY(user_id,source_id,event_type)
            );
            CREATE TABLE IF NOT EXISTS notification_retention_gaps (
                user_id TEXT PRIMARY KEY, evicted INTEGER NOT NULL DEFAULT 0,
                last_eviction REAL NOT NULL, until_time REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_resume_fences (
                registration TEXT NOT NULL, user_id TEXT NOT NULL,
                stream TEXT NOT NULL, position INTEGER NOT NULL,
                PRIMARY KEY(registration,user_id)
            );
            CREATE TABLE IF NOT EXISTS notification_result_revisions (
                registration TEXT NOT NULL, user_id TEXT NOT NULL, revision TEXT NOT NULL,
                PRIMARY KEY(registration,user_id,revision)
            );
            CREATE TABLE IF NOT EXISTS notification_read_budget (
                scope TEXT NOT NULL, character_id TEXT NOT NULL, charged REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS notification_budget_time ON notification_read_budget(charged);
        """)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def configure(self, registration: Registration) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT signature FROM notification_registrations WHERE id=?", (registration.id,)).fetchone()
            if row is not None and row["signature"] != registration.signature:
                raise failure("notification_registration_changed")
            if row is None:
                if db.execute("SELECT count(*) FROM notification_registrations").fetchone()[0] >= 1024:
                    raise failure("notification_registration_limit")
                db.execute("INSERT INTO notification_registrations(id,signature) VALUES (?,?)", (registration.id, registration.signature))

    def registration(self, registration: str) -> Json:
        row = self.db.execute("SELECT * FROM notification_registrations WHERE id=?", (registration,)).fetchone()
        if row is None:
            raise failure("notification_registration_unavailable")
        return dict(row)

    def pin(self, registration: Registration, identity: str, binding_signature: str) -> None:
        with self.transaction() as db:
            row = self.registration(registration.id)
            if row["signature"] != registration.signature or row["identity"] not in (None, identity) or row["binding_signature"] not in (None, binding_signature):
                raise failure("notification_target_changed")
            if row["identity"] == identity and row["binding_signature"] == binding_signature:
                return
            db.execute("UPDATE notification_registrations SET identity=?,binding_signature=? WHERE id=?", (identity, binding_signature, registration.id))

    def preference(self, user_id: str, source_id: str, event_type: str) -> bool | None:
        row = self.db.execute("SELECT enabled FROM notification_preferences WHERE user_id=? AND source_id=? AND event_type=?", (user_id, source_id, event_type)).fetchone()
        return bool(row[0]) if row is not None else None

    def set_preference(self, user_id: str, source_id: str, event_type: str, enabled: bool, *, fences: tuple[tuple[str, str, int], ...] = ()) -> None:
        with self.transaction() as db:
            db.execute("""INSERT INTO notification_preferences VALUES (?,?,?,?,?)
                ON CONFLICT(user_id,source_id,event_type) DO UPDATE SET enabled=excluded.enabled,changed=excluded.changed""",
                (user_id, source_id, event_type, int(enabled), self.clock()))
            for registration, stream, position in fences:
                db.execute("""INSERT INTO notification_resume_fences VALUES (?,?,?,?)
                    ON CONFLICT(registration,user_id) DO UPDATE SET stream=excluded.stream,position=excluded.position""",
                    (registration, user_id, stream, position))

    def decision(self, registration: Registration, user_id: str) -> Decision:
        preference = self.preference(user_id, registration.source_id, registration.event_type)
        # state-onlyは管理Policyであり、UIの通知ONでユーザー通知へ昇格しない。
        if registration.decision == "state-only":
            return "state-only"
        if preference is None:
            return registration.decision
        return "notify" if preference else "ignore"

    def _prune(self, db: sqlite3.Connection) -> None:
        now = self.clock()
        db.execute("DELETE FROM notifications WHERE expires<=?", (now,))
        db.execute("DELETE FROM notification_retention_gaps WHERE until_time<=?", (now,))
        users = db.execute("SELECT user_id FROM notifications GROUP BY user_id HAVING count(*)>?", (self.limits.max_per_user,)).fetchall()
        for user in users:
            rows = db.execute("SELECT id,expires FROM notifications WHERE user_id=? ORDER BY created DESC,id DESC LIMIT -1 OFFSET ?", (user[0], self.limits.max_per_user)).fetchall()
            if not rows:
                continue
            # 期間内の削除が残る間だけ、本文・出典を含まない件数を保持する。
            db.execute("""INSERT INTO notification_retention_gaps VALUES (?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET evicted=evicted+excluded.evicted,
                last_eviction=excluded.last_eviction,until_time=max(until_time,excluded.until_time)""",
                (user[0], len(rows), now, max(r["expires"] for r in rows)))
            db.executemany("DELETE FROM notifications WHERE id=?", ((r["id"],) for r in rows))
        db.execute("DELETE FROM notification_read_budget WHERE charged<=?", (now - 60,))

    def prune(self) -> None:
        with self.transaction() as db:
            self._prune(db)

    def apply_batch(self, registration: Registration, stream: str, end_position: int, events: tuple[Json, ...], *, gaps: tuple[Json, ...] = ()) -> None:
        """通知保存と通知判定の位置を同じtransactionへ置き、upstream ACKは呼出側が後で行う。"""
        with self.transaction() as db:
            current = self.registration(registration.id)
            floor = current["position"] if current["stream"] == stream else -1
            if end_position < floor:
                raise failure("notification_delivery_rewound")
            now = self.clock()
            last: Json = {}
            last_decision: Decision = "ignore"
            revision_limited = False
            for event in events:
                if event["position"] <= floor:
                    continue
                value = event["metadata"]
                if value["type"] != registration.event_type:
                    continue
                match = json.loads(registration.match_json)
                if any(value.get(k) != v for k, v in match.items()):
                    continue
                last = value
                for user in registration.recipients:
                    decision = self.decision(registration, user) if user in event.get("permitted_users", registration.recipients) else "ignore"
                    last_decision = decision
                    fence = db.execute("SELECT stream,position FROM notification_resume_fences WHERE registration=? AND user_id=?", (registration.id, user)).fetchone()
                    if fence is not None and (fence["stream"] != stream or event["position"] <= fence["position"]):
                        decision = "ignore"
                    if decision != "notify":
                        continue
                    if registration.kind == "result":
                        # 単発Taskの結果revisionは通知削除後も再作成しない。
                        revision = value.get("revision")
                        if not isinstance(revision, str):
                            continue
                        revision_key = digest([value.get("task_ref"), value.get("execution_ref"), revision])
                        if db.execute("SELECT 1 FROM notification_result_revisions WHERE registration=? AND user_id=? AND revision=?", (registration.id, user, revision_key)).fetchone():
                            continue
                        if db.execute("SELECT count(*) FROM notification_result_revisions WHERE registration=? AND user_id=?", (registration.id, user)).fetchone()[0] >= 1024:
                            revision_limited = True
                            continue
                        db.execute("INSERT INTO notification_result_revisions VALUES (?,?,?)", (registration.id, user, revision_key))
                    notification_id = digest([registration.id, user, event["key"]])
                    db.execute("""INSERT OR IGNORE INTO notifications
                        (id,registration,user_id,source_id,character_id,event_type,event_key,metadata,created,expires)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""", (notification_id, registration.id, user,
                        registration.source_id, registration.character_id, registration.event_type,
                        event["key"], encode(value), now, now + self.limits.retention_seconds))
            db.execute("DELETE FROM notification_resume_fences WHERE registration=? AND stream=? AND position<=?", (registration.id, stream, end_position))
            # EventRuntimeが世代変更を確認したbaselineでは、旧世代の抑止位置を失効させる。
            # 単なる別世代の履歴再読では解除せず、OFF中の履歴を通知しない。
            if any(gap.get("epoch_changed") is True for gap in gaps):
                db.execute("DELETE FROM notification_resume_fences WHERE registration=? AND stream!=?", (registration.id, stream))
            # 再配送の同じbatchで欠落件数・観測を二重加算しない。
            moved = current["stream"] != stream or end_position > floor
            gap_count = len(gaps) + int(revision_limited) if moved else 0
            reason = ("notification_revision_limit" if revision_limited else
                      gaps[-1].get("reason", "event_unavailable") if gaps and moved else current["last_gap"])
            allowed_reasons = {"history_unavailable", "invalid_event", "privacy_changed", "event_unavailable", "notification_revision_limit"}
            reason = reason if reason in allowed_reasons else "event_unavailable" if reason else None
            observed = encode(last) if last_decision == "state-only" else current["observed"]
            db.execute("""UPDATE notification_registrations SET stream=?,position=?,gap_count=gap_count+?,
                last_gap=?,last_decision=?,observed=? WHERE id=?""",
                (stream, end_position, gap_count, reason, last_decision, observed, registration.id))
            self._prune(db)

    def get(self, notification_id: str, user_id: str) -> Json:
        row = self.db.execute("SELECT * FROM notifications WHERE id=? AND user_id=? AND expires>?",
                              (notification_id, user_id, self.clock())).fetchone()
        if row is None:
            raise failure("notification_not_found")
        return dict(row)

    def listing(self, user_id: str, allowed: set[str], *, source_id: str | None = None, character_id: str | None = None,
                unread: bool = False, hidden: bool = False, offset: int = 0, limit: int = 50) -> Json:
        if not 1 <= limit <= 100 or not 0 <= offset <= self.limits.max_per_user:
            raise failure("invalid_notification_page")
        now = self.clock()
        predicates = ["user_id=?", "expires>?"]
        parameters: list[object] = [user_id, now]
        predicates.append("state='hidden'" if hidden else "state!='hidden'")
        for key, value in (("source_id", source_id), ("character_id", character_id)):
            if value is not None:
                predicates.append(f"{key}=?")
                parameters.append(value)
        if unread:
            predicates.append("state='unread'")
        if allowed:
            predicates.append("registration IN (" + ",".join("?" for _ in allowed) + ")")
            parameters.extend(sorted(allowed))
        else:
            predicates.append("0")
        where = " AND ".join(predicates)
        rows = self.db.execute(f"SELECT * FROM notifications WHERE {where} ORDER BY created DESC,id DESC LIMIT ? OFFSET ?", (*parameters, limit, offset)).fetchall()
        total = self.db.execute(f"SELECT count(*) FROM notifications WHERE {where}", parameters).fetchone()[0]
        # badgeは絞り込みと独立し、権限のある通知だけを数える。
        badge = 0
        if allowed:
            slots = ",".join("?" for _ in allowed)
            badge = self.db.execute(f"SELECT count(*) FROM notifications WHERE user_id=? AND state='unread' AND expires>? AND registration IN ({slots})", (user_id, now, *sorted(allowed))).fetchone()[0]
        gap = self.db.execute("SELECT evicted,last_eviction FROM notification_retention_gaps WHERE user_id=? AND until_time>?", (user_id, now)).fetchone()
        return {"items": [dict(r) for r in rows], "total": total, "unread_count": badge,
                "next_offset": offset + len(rows) if offset + len(rows) < total else None,
                "retention": {"days": self.limits.retention_seconds / 86400, "max_per_user": self.limits.max_per_user,
                              "history_incomplete": gap is not None, "evicted_count": gap[0] if gap else 0}}

    def set_state(self, notification_id: str, user_id: str, state: Literal["read", "unread", "hidden"], expected_version: int) -> Json:
        if state not in {"read", "unread", "hidden"}:
            raise failure("invalid_notification_state")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM notifications WHERE id=? AND user_id=? AND expires>?",
                             (notification_id, user_id, self.clock())).fetchone()
            if row is None:
                raise failure("notification_not_found")
            if row["version"] != expected_version:
                raise failure("notification_state_conflict")
            db.execute("UPDATE notifications SET state=?,version=version+1 WHERE id=? AND user_id=?", (state, notification_id, user_id))
            return dict(db.execute("SELECT * FROM notifications WHERE id=? AND user_id=?",
                                   (notification_id, user_id)).fetchone())

    def charge_read(self, registration: Registration) -> None:
        # 再試行・複数端末・Backend再起動でも登録済みの安定単位で同じ窓へ計上する。
        scope = digest([registration.source_id, registration.character_id, registration.scope])
        with self.transaction() as db:
            now = self.clock()
            db.execute("DELETE FROM notification_read_budget WHERE charged<=?", (now - 60,))
            scope_count = db.execute("SELECT count(*) FROM notification_read_budget WHERE scope=?", (scope,)).fetchone()[0]
            character_count = db.execute("SELECT count(*) FROM notification_read_budget WHERE character_id=?", (registration.character_id,)).fetchone()[0]
            if scope_count >= self.limits.scope_reads_per_minute or character_count >= self.limits.character_reads_per_minute:
                raise failure("notification_budget_exceeded")
            db.execute("INSERT INTO notification_read_budget VALUES (?,?,?)", (scope, registration.character_id, now))

    def close(self) -> None:
        self.db.close()

"""承認状態のSQLite正本。単回許可はtransaction内で消費し、場面間で共有しない。"""

from __future__ import annotations

import os
import json
import sqlite3
import time
from uuid import uuid4
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.external_mcp.models import Json, MCPFailure, encode

from .models import (
    ApprovalChoice,
    ApprovalKey,
    ExecutionScene,
    OperationGroup,
    Permission,
)


@dataclass(frozen=True)
class PermissionState:
    permission: Permission
    remaining: int = 0

    @property
    def allowed(self) -> bool:
        return self.permission == Permission.ALWAYS or self.remaining > 0


@dataclass(frozen=True)
class ConfirmationRequest:
    id: str
    key: ApprovalKey
    character_id: str
    session_id: str
    loop_id: str
    fingerprint: str
    preview: Json
    created_at: float
    wait_until: float
    waiting: bool
    choice: ApprovalChoice | None
    once_reserved: bool = False

    def public(self) -> Json:
        return {
            "id": self.id,
            "connection_id": self.key.connection_id,
            "operation_group": self.key.group,
            "scene": self.key.scene,
            "preview": self.preview,
            "created_at": self.created_at,
            "wait_until": self.wait_until,
            "waiting": self.waiting,
            "choice": self.choice,
        }


class ActionStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS action_permissions (
                connection_id TEXT NOT NULL, identity TEXT NOT NULL,
                operation_group TEXT NOT NULL CHECK(operation_group IN ('normal','high_impact')),
                scene TEXT NOT NULL CHECK(scene IN ('conversation','autonomous')),
                permission TEXT NOT NULL CHECK(permission IN ('unapproved','always','denied')),
                remaining INTEGER NOT NULL DEFAULT 0 CHECK(remaining >= 0),
                PRIMARY KEY(connection_id, identity, operation_group, scene)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS action_confirmations (
                id TEXT PRIMARY KEY, connection_id TEXT NOT NULL, identity TEXT NOT NULL,
                operation_group TEXT NOT NULL, scene TEXT NOT NULL,
                character_id TEXT NOT NULL, session_id TEXT NOT NULL, loop_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, preview TEXT NOT NULL,
                created_at REAL NOT NULL, wait_until REAL NOT NULL,
                waiting INTEGER NOT NULL DEFAULT 1, choice TEXT,
                UNIQUE(loop_id, fingerprint)
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(action_confirmations)")}
            if "once_reserved" not in columns:
                db.execute("ALTER TABLE action_confirmations ADD COLUMN once_reserved INTEGER NOT NULL DEFAULT 0")

            db.execute("""CREATE INDEX IF NOT EXISTS action_confirmations_page
                ON action_confirmations(created_at DESC, id DESC)""")
            db.execute("""CREATE INDEX IF NOT EXISTS action_confirmations_unanswered_page
                ON action_confirmations(created_at DESC, id DESC) WHERE choice IS NULL""")
            db.execute("""CREATE INDEX IF NOT EXISTS action_confirmations_reservations
                ON action_confirmations(connection_id, identity, operation_group, scene, wait_until)
                WHERE once_reserved=1 AND waiting=1""")

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _state(db: sqlite3.Connection, key: ApprovalKey) -> PermissionState:
        row = db.execute(
            """SELECT permission, remaining FROM action_permissions
            WHERE connection_id=? AND identity=? AND operation_group=? AND scene=?""",
            key.values(),
        ).fetchone()
        if row is None:
            return PermissionState(
                Permission.ALWAYS
                if key.group == OperationGroup.NORMAL
                else Permission.UNAPPROVED
            )
        return PermissionState(Permission(row["permission"]), row["remaining"])

    def state(self, key: ApprovalKey) -> PermissionState:
        with self.transaction() as db:
            return self._state(db, key)

    @staticmethod
    def _write(
        db: sqlite3.Connection, key: ApprovalKey, state: PermissionState
    ) -> None:
        db.execute(
            """INSERT INTO action_permissions VALUES (?,?,?,?,?,?)
            ON CONFLICT(connection_id,identity,operation_group,scene)
            DO UPDATE SET permission=excluded.permission, remaining=excluded.remaining""",
            (*key.values(), state.permission, state.remaining),
        )

    def _choose(
        self, db: sqlite3.Connection, key: ApprovalKey, choice: ApprovalChoice,
        *, reserve_once: bool = False,
    ) -> None:
        if choice == ApprovalChoice.REJECT:
            if key.scene == ExecutionScene.AUTONOMOUS:
                self._write(db, key, PermissionState(Permission.DENIED))
            return
        state = self._state(db, key)
        self._write(
            db,
            key,
            PermissionState(Permission.ALWAYS)
            if choice == ApprovalChoice.ALWAYS
            else PermissionState(Permission.UNAPPROVED, state.remaining + int(not reserve_once)),
        )

    def choose(self, key: ApprovalKey, choice: ApprovalChoice) -> None:
        """管理境界用。キューの回答は後続の要求IDによる重複排除を経由する。"""
        with self.transaction() as db:
            self._choose(db, key, choice)

    def settings(self, key: ApprovalKey) -> Json:
        return self.settings_many([key])[key]

    def settings_many(self, keys: Iterable[ApprovalKey]) -> dict[ApprovalKey, Json]:
        """複数承認範囲を同じ読み取りsnapshotから取得する。"""
        result: dict[ApprovalKey, Json] = {}
        with self.transaction(write=False) as db:
            now = self.clock()
            for key in keys:
                state = self._state(db, key)
                reserved = db.execute(
                    """SELECT COUNT(*) FROM action_confirmations
                    WHERE connection_id=? AND identity=? AND operation_group=? AND scene=?
                    AND once_reserved=1 AND waiting=1 AND wait_until>?""",
                    (*key.values(), now),
                ).fetchone()[0]
                result[key] = {"permission": state.permission, "remaining": state.remaining,
                               "reserved": reserved}
        return result

    def set_permission(self, key: ApprovalKey, permission: Permission) -> tuple[str, ...]:
        """保存設定の置換。dispatchの消費と同じtransaction境界で未使用許可を解除する。"""
        if permission == Permission.DENIED and key.scene == ExecutionScene.CONVERSATION:
            raise MCPFailure("policy", "conversation_denial_is_request_scoped")
        with self.transaction() as db:
            self._write(db, key, PermissionState(permission))
            ended: tuple[str, ...] = ()
            if permission != Permission.ALWAYS:
                ended = tuple(row["id"] for row in db.execute(
                    """SELECT id FROM action_confirmations
                    WHERE connection_id=? AND identity=? AND operation_group=? AND scene=?
                    AND choice IS NOT NULL AND waiting=1""", key.values(),
                ))
                # 既に回答した古い要求は、後の再承認でも再開させない。
                # 未回答キューは保持し、次の明示回答を可能にする。
                db.execute(
                    """UPDATE action_confirmations SET once_reserved=0,
                    waiting=CASE WHEN choice IS NOT NULL THEN 0 ELSE waiting END
                    WHERE connection_id=? AND identity=? AND operation_group=? AND scene=?""",
                    key.values(),
                )
            return ended

    def request_page(
        self, *, unanswered: bool = True, before: str | None = None, limit: int = 50
    ) -> tuple[tuple[ConfirmationRequest, ...], str | None]:
        """管理キューは作成時刻とIDでページ化し、未回答を件数上限で隠さない。"""
        with self.transaction(write=False) as db:
            cursor = None
            if before is not None:
                cursor = db.execute(
                    "SELECT created_at,id FROM action_confirmations WHERE id=?", (before,)
                ).fetchone()
                if cursor is None:
                    raise MCPFailure("policy", "unknown_confirmation_cursor")
            unanswered_filter = "choice IS NULL AND " if unanswered else ""
            rows = db.execute(
                f"""SELECT * FROM action_confirmations
                WHERE {unanswered_filter}1=1
                AND (? IS NULL OR created_at<? OR (created_at=? AND id<?))
                ORDER BY created_at DESC,id DESC LIMIT ?""",
                (before, cursor["created_at"] if cursor else None,
                 cursor["created_at"] if cursor else None, before, limit + 1),
            ).fetchall()
            page = tuple(self._request(row) for row in rows[:limit])
            return page, page[-1].id if len(rows) > limit else None

    def consume(self, key: ApprovalKey) -> bool:
        with self.transaction() as db:
            return self._consume(db, key)

    def _consume(self, db: sqlite3.Connection, key: ApprovalKey) -> bool:
        state = self._state(db, key)
        if state.permission == Permission.ALWAYS:
            return True
        if state.permission == Permission.DENIED or state.remaining == 0:
            return False
        self._write(
            db, key, PermissionState(Permission.UNAPPROVED, state.remaining - 1)
        )
        return True

    def consume_request(
        self, key: ApprovalKey, request_id: str | None, now: float
    ) -> bool:
        with self.transaction() as db:
            if request_id is not None:
                row = db.execute(
                    "SELECT * FROM action_confirmations WHERE id=?", (request_id,)
                ).fetchone()
                if row is None:
                    return False
                request = self._request(row)
                if (
                    request.key != key
                    or not request.waiting
                    or now >= request.wait_until
                    or request.choice == ApprovalChoice.REJECT
                ):
                    return False
                if request.once_reserved:
                    if self._state(db, key).permission == Permission.DENIED:
                        return False
                    db.execute(
                        "UPDATE action_confirmations SET waiting=0,once_reserved=0 WHERE id=?",
                        (request_id,),
                    )
                    return True
            if not self._consume(db, key):
                return False
            if request_id is not None:
                db.execute(
                    "UPDATE action_confirmations SET waiting=0 WHERE id=?",
                    (request_id,),
                )
            return True

    @staticmethod
    def _request(row: sqlite3.Row) -> ConfirmationRequest:
        return ConfirmationRequest(
            row["id"],
            ApprovalKey(
                row["connection_id"],
                row["identity"],
                OperationGroup(row["operation_group"]),
                ExecutionScene(row["scene"]),
            ),
            row["character_id"],
            row["session_id"],
            row["loop_id"],
            row["fingerprint"],
            json.loads(row["preview"]),
            row["created_at"],
            row["wait_until"],
            bool(row["waiting"]),
            ApprovalChoice(row["choice"]) if row["choice"] else None,
            bool(row["once_reserved"]),
        )

    def enqueue(
        self,
        key: ApprovalKey,
        *,
        character_id: str,
        session_id: str,
        loop_id: str,
        fingerprint: str,
        preview: Json,
        wait_seconds: float,
        now: float | None = None,
    ) -> ConfirmationRequest:
        created = self.clock() if now is None else now
        with self.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO action_confirmations
                (id,connection_id,identity,operation_group,scene,character_id,session_id,
                 loop_id,fingerprint,preview,created_at,wait_until,waiting,choice)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,NULL)""",
                (
                    str(uuid4()),
                    *key.values(),
                    character_id,
                    session_id,
                    loop_id,
                    fingerprint,
                    encode(preview),
                    created,
                    created + wait_seconds,
                ),
            )
            row = db.execute(
                "SELECT * FROM action_confirmations WHERE loop_id=? AND fingerprint=?",
                (loop_id, fingerprint),
            ).fetchone()
            assert row is not None
            return self._request(row)

    def request(self, request_id: str) -> ConfirmationRequest:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM action_confirmations WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise MCPFailure("policy", "unknown_confirmation")
            return self._request(row)

    def requests(
        self, *, character_id: str | None = None, session_id: str | None = None
    ) -> tuple[ConfirmationRequest, ...]:
        with self.transaction() as db:
            rows = db.execute(
                """SELECT * FROM action_confirmations
                WHERE (? IS NULL OR character_id=?) AND (? IS NULL OR session_id=?)
                ORDER BY created_at DESC LIMIT 200""",
                (character_id, character_id, session_id, session_id),
            ).fetchall()
            return tuple(self._request(row) for row in rows)

    def answer(self, request_id: str, choice: ApprovalChoice) -> ConfirmationRequest:
        # 回答の重複送信と単回許可の発行を同じtransactionに置く。
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM action_confirmations WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise MCPFailure("policy", "unknown_confirmation")
            request = self._request(row)
            if request.choice is not None:
                if request.choice != choice:
                    raise MCPFailure("policy", "confirmation_already_answered")
                return request
            current = self.clock()
            reserve_once = (
                choice == ApprovalChoice.ONCE
                and request.waiting and current < request.wait_until
            )
            # 待機中の単回許可は表示した要求だけへ予約する。遅い回答だけが将来の共通枠になる。
            self._choose(db, request.key, choice, reserve_once=reserve_once)
            db.execute(
                """UPDATE action_confirmations SET choice=?,once_reserved=?,
                waiting=CASE WHEN wait_until<=? THEN 0 ELSE waiting END WHERE id=?""",
                (choice, int(reserve_once), current, request_id),
            )
            row = db.execute(
                "SELECT * FROM action_confirmations WHERE id=?", (request_id,)
            ).fetchone()
            assert row is not None
            return self._request(row)

    def end_wait(self, request_id: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE action_confirmations SET waiting=0,once_reserved=0 WHERE id=?", (request_id,)
            )

    def end_loop(self, loop_id: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE action_confirmations SET waiting=0,once_reserved=0 WHERE loop_id=?", (loop_id,)
            )

    def detach_waiters(self) -> None:
        """runtime起動時に一度だけ呼ぶ。保存済みキュー・回答は消さない。"""
        with self.transaction() as db:
            db.execute("UPDATE action_confirmations SET waiting=0,once_reserved=0 WHERE waiting=1")

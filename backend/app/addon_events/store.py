"""期限・容量付きEventバッファと独立したconsumer進捗。"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from app.external_mcp import ExecutionContext
from app.external_mcp.models import Json, digest, encode
from .contracts import Baseline, Event, Page, Source, failure, token


def context_json(context: ExecutionContext) -> str:
    return encode({
        "character_id": context.character_id, "user_id": context.user_id,
        "binding_id": context.binding_id, "session_id": context.session_id,
    })


class EventStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise failure("unsafe_event_store")
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            PRAGMA secure_delete=ON;
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS event_sources (
                id TEXT PRIMARY KEY, signature TEXT NOT NULL, identity TEXT,
                epoch TEXT, cursor TEXT, position INTEGER NOT NULL DEFAULT 0,
                floor INTEGER NOT NULL DEFAULT 0, stream INTEGER NOT NULL DEFAULT 0,
                baseline TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'idle',
                reason TEXT, gap_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS event_buffer (
                source TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
                position INTEGER NOT NULL, cursor TEXT NOT NULL, event_key TEXT NOT NULL,
                metadata TEXT NOT NULL, reason TEXT, acquired REAL NOT NULL, bytes INTEGER NOT NULL,
                PRIMARY KEY(source,position), UNIQUE(source,event_key)
            );
            CREATE INDEX IF NOT EXISTS event_buffer_expiry ON event_buffer(source,acquired);
            CREATE TABLE IF NOT EXISTS event_consumers (
                source TEXT NOT NULL REFERENCES event_sources(id) ON DELETE CASCADE,
                id TEXT NOT NULL, context TEXT NOT NULL, epoch TEXT, cursor TEXT,
                position INTEGER NOT NULL DEFAULT 0, stream INTEGER NOT NULL DEFAULT 0,
                initial INTEGER NOT NULL DEFAULT 1, initial_state TEXT NOT NULL DEFAULT '{}', receipt TEXT, offered TEXT,
                gap_count INTEGER NOT NULL DEFAULT 0, last_gap TEXT,
                PRIMARY KEY(source,id)
            );
        """)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def configure(self, source: Source) -> None:
        row = self.db.execute("SELECT signature FROM event_sources WHERE id=?", (source.id,)).fetchone()
        if row is not None and row["signature"] != source.signature:
            raise failure("event_source_registration_changed")
        if row is None:
            if self.db.execute("SELECT count(*) FROM event_sources").fetchone()[0] >= 32:
                raise failure("event_source_limit")
            self.db.execute("INSERT INTO event_sources(id,signature) VALUES (?,?)", (source.id, source.signature))

    def state(self, source: str) -> Json:
        row = self.db.execute("SELECT * FROM event_sources WHERE id=?", (source,)).fetchone()
        if row is None:
            raise failure("unknown_event_source")
        return dict(row)

    def pin_identity(self, source: str, identity: str) -> None:
        current = self.state(source)["identity"]
        if current is not None and current != identity:
            raise failure("event_connection_identity_changed")
        self.db.execute("UPDATE event_sources SET identity=? WHERE id=?", (identity, source))

    def set_status(self, source: str, status: str, reason: str | None = None) -> None:
        self.db.execute("UPDATE event_sources SET status=?,reason=? WHERE id=?", (status, reason, source))

    def consumers(self, source: str) -> list[Json]:
        return [dict(r) for r in self.db.execute("SELECT * FROM event_consumers WHERE source=?", (source,))]

    def consumer(self, source: str, consumer: str) -> Json:
        row = self.db.execute("SELECT * FROM event_consumers WHERE source=? AND id=?", (source, consumer)).fetchone()
        if row is None:
            raise failure("unknown_event_consumer")
        return dict(row)

    def register(self, source: str, consumer: str, context: ExecutionContext) -> None:
        token(consumer)
        encoded = context_json(context)
        with self.transaction() as db:
            previous = db.execute("SELECT context FROM event_consumers WHERE source=? AND id=?", (source, consumer)).fetchone()
            if previous is not None:
                if previous["context"] != encoded:
                    raise failure("event_consumer_identity_changed")
                return
            if db.execute("SELECT count(*) FROM event_consumers").fetchone()[0] >= 1024:
                raise failure("event_consumer_limit")
            state = self.state(source)
            db.execute(
                "INSERT INTO event_consumers(source,id,context,epoch,cursor,position,stream) VALUES (?,?,?,?,?,?,?)",
                (source, consumer, encoded, state["epoch"], state["cursor"], state["position"], state["stream"]),
            )

    def unregister(self, source: str, consumer: str) -> None:
        self.db.execute("DELETE FROM event_consumers WHERE source=? AND id=?", (source, consumer))
        if not self.consumers(source):
            self.set_status(source, "idle")

    def baseline(self, source: str, baseline: Baseline, *, gap: bool) -> None:
        with self.transaction() as db:
            stream = self.state(source)["stream"] + 1
            db.execute("DELETE FROM event_buffer WHERE source=?", (source,))
            db.execute(
                """UPDATE event_sources SET epoch=?,cursor=?,position=?,floor=?,stream=?,
                   baseline=?,status='ready',reason=?,gap_count=gap_count+? WHERE id=?""",
                (baseline.epoch, baseline.cursor, baseline.position, baseline.position, stream,
                 baseline.metadata_json, "history_gap" if gap else None, int(gap), source),
            )
            # 初回購読者だけを初期位置へ揃える。既存consumerの未処理位置は進めない。
            db.execute(
                """UPDATE event_consumers SET epoch=?,cursor=?,position=?,stream=?
                   WHERE source=? AND epoch IS NULL""",
                (baseline.epoch, baseline.cursor, baseline.position, stream, source),
            )

    def commit_page(self, source: Source, page: Page, *, expected_cursor: str) -> None:
        with self.transaction() as db:
            state = self.state(source.id)
            if state["cursor"] != expected_cursor or state["epoch"] != page.epoch or page.gap:
                raise failure("event_ingestion_conflict")
            for event in page.events:
                size = len(event.metadata_json.encode()) + len(event.cursor.encode()) + 128
                try:
                    db.execute(
                        """INSERT INTO event_buffer(source,position,cursor,event_key,metadata,reason,acquired,bytes)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (source.id, event.position, event.cursor, event.key,
                         event.metadata_json, event.reason, self.clock(), size),
                    )
                except sqlite3.IntegrityError:
                    raise failure("conflicting_event_id") from None
            db.execute(
                "UPDATE event_sources SET cursor=?,position=?,status='ready',reason=NULL WHERE id=?",
                (page.cursor, page.position, source.id),
            )
            self._prune(source, db)

    def _prune(self, source: Source, db: sqlite3.Connection) -> None:
        state = self.state(source.id)
        floor = state["floor"]
        cutoff = self.clock() - source.limits.retention_seconds
        expiry = db.execute(
            "SELECT max(position) FROM event_buffer WHERE source=? AND acquired<=?", (source.id, cutoff)
        ).fetchone()[0]
        if expiry is not None:
            floor = max(floor, expiry)
        rows = db.execute(
            "SELECT position,bytes FROM event_buffer WHERE source=? AND position>? ORDER BY position DESC",
            (source.id, floor),
        )
        total = 0
        for count, row in enumerate(rows, 1):
            total += row["bytes"]
            if count > source.limits.max_events or total > source.limits.max_bytes:
                floor = max(floor, row["position"])
                break
        db.execute("DELETE FROM event_buffer WHERE source=? AND position<=?", (source.id, floor))
        db.execute("UPDATE event_sources SET floor=? WHERE id=?", (floor, source.id))

    def prune(self, source: Source) -> None:
        with self.transaction() as db:
            self._prune(source, db)

    def buffered(self, source: Source, after: int) -> tuple[Event, ...]:
        self.prune(source)
        return tuple(
            Event(r["position"], r["cursor"], r["event_key"], r["metadata"], r["reason"])
            for r in self.db.execute(
                "SELECT * FROM event_buffer WHERE source=? AND position>? ORDER BY position LIMIT ?",
                (source.id, after, source.limits.page_size),
            )
        )

    def offer(self, source: str, consumer: str, *, epoch: str, cursor: str, position: int,
              stream: int, gaps: list[Json], initial: bool) -> str:
        receipt = str(uuid4())
        offered = encode({"epoch": epoch, "cursor": cursor, "position": position,
                         "stream": stream, "gaps": gaps, "initial": initial})
        self.db.execute(
            "UPDATE event_consumers SET receipt=?,offered=? WHERE source=? AND id=?",
            (receipt, offered, source, consumer),
        )
        return receipt

    def acknowledge(self, source: str, consumer: str, receipt: str, *, accept_gap: bool) -> None:
        with self.transaction() as db:
            current = self.consumer(source, consumer)
            if current["receipt"] != receipt or current["offered"] is None:
                raise failure("stale_event_receipt")
            offered = json.loads(current["offered"])
            if offered["gaps"] and not accept_gap:
                raise failure("event_gap_ack_required")
            previous_gap = current["last_gap"]
            if offered["gaps"]:
                # 欠落は正常処理と別に保持する。無制限の監査履歴にはしない。
                previous_gap = encode(offered["gaps"])
            db.execute(
                """UPDATE event_consumers SET epoch=?,cursor=?,position=?,stream=?,initial=0,
                   receipt=NULL,offered=NULL,gap_count=gap_count+?,last_gap=?
                   WHERE source=? AND id=?""",
                (offered["epoch"], offered["cursor"], offered["position"], offered["stream"],
                 len(offered["gaps"]), previous_gap, source, consumer),
            )

    def diagnostics(self, source: str) -> Json:
        state = self.state(source)
        count, size = self.db.execute(
            "SELECT count(*),coalesce(sum(bytes),0) FROM event_buffer WHERE source=?", (source,)
        ).fetchone()
        return {"source_key": digest(source), "status": state["status"], "reason": state["reason"],
                "buffer_count": count, "buffer_bytes": size, "gap_count": state["gap_count"],
                "consumer_count": len(self.consumers(source))}

    def close(self) -> None:
        self.db.close()

    def initial(self, source: str, consumer: str, baseline: Baseline) -> None:
        self.db.execute(
            """UPDATE event_consumers SET epoch=?,cursor=?,position=?,stream=?,initial_state=?
               WHERE source=? AND id=?""",
            (baseline.epoch, baseline.cursor, baseline.position, self.state(source)["stream"],
             baseline.metadata_json, source, consumer),
        )

    def suspend_unconfigured(self, configured: set[str]) -> None:
        # 設定から外れたsourceのpayloadは保持しない。consumer進捗は解除扱いにせず残す。
        with self.transaction() as db:
            for row in db.execute("SELECT id FROM event_sources").fetchall():
                if row["id"] not in configured:
                    db.execute("DELETE FROM event_buffer WHERE source=?", (row["id"],))
                    db.execute("UPDATE event_sources SET floor=position,status='suspended',reason='not_configured' WHERE id=?",
                               (row["id"],))

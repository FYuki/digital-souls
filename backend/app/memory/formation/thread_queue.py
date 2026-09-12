"""スレッド更新を合流し、未完了revisionを再取得できる永続キュー。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
import sqlite3
import math
from uuid import UUID, uuid4

from app.conversation_history._sqlite import (
    SqliteSession,
    TURN_COLUMNS,
    format_datetime,
    turn_from_row,
)
from app.conversation_history.models import ConversationTurn, TurnStatus


class ThreadOutcome(str, Enum):
    SAVED = "SAVED"
    REJECTED = "REJECTED"
    EMPTY = "EMPTY"
    MIXED = "MIXED"


class StaleThreadSnapshot(RuntimeError):
    pass


@dataclass(frozen=True)
class ThreadLease:
    character_id: str
    conversation_id: UUID
    revision: int
    token: UUID


@dataclass(frozen=True)
class ThreadSource:
    turn: ConversationTurn
    revision: int


@dataclass(frozen=True)
class ThreadSnapshot:
    lease: ThreadLease
    sources: tuple[ThreadSource, ...]
    retention_cutoff: datetime


class ThreadFormationQueue:
    def __init__(
        self,
        *,
        database_path: Path,
        clock: Callable[[], datetime],
        retention: timedelta,
        lease_seconds: float = 120,
        retry_seconds: float = 5,
        connection_factory: Callable[[Path], sqlite3.Connection] = sqlite3.connect,
    ) -> None:
        if (
            not math.isfinite(lease_seconds) or not math.isfinite(retry_seconds)
            or lease_seconds <= 0 or retry_seconds <= 0 or retention <= timedelta(0)
        ):
            raise ValueError("queue intervals must be positive")
        self._database = SqliteSession(database_path, connection_factory)
        self._clock = clock
        self._retention = retention
        self._lease_seconds = lease_seconds
        self._retry_seconds = retry_seconds

    @property
    def renewal_interval_seconds(self) -> float:
        return self._lease_seconds / 3

    def claim(self) -> ThreadLease | None:
        now = self._clock().timestamp()
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT j.character_id, j.conversation_id, j.revision
                FROM memory_thread_jobs j
                WHERE j.revision > j.completed_revision AND j.next_attempt_at <= ?
                AND (j.lease_token IS NULL OR j.lease_until <= ?)
                AND NOT EXISTS (SELECT 1 FROM conversation_turns t WHERE t.character_id = j.character_id
                    AND t.conversation_id = j.conversation_id AND t.status = 'processing')
                ORDER BY j.updated_at, j.character_id, j.conversation_id LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            lease = ThreadLease(
                row["character_id"],
                UUID(row["conversation_id"]),
                row["revision"],
                uuid4(),
            )
            connection.execute(
                "UPDATE memory_thread_jobs SET lease_token = ?, leased_revision = ?, lease_until = ?, attempts = attempts + 1 WHERE character_id = ? AND conversation_id = ?",
                (
                    str(lease.token),
                    lease.revision,
                    now + self._lease_seconds,
                    lease.character_id,
                    str(lease.conversation_id),
                ),
            )
            return lease

    def renew(self, lease: ThreadLease) -> bool:
        with self._database.transaction() as connection:
            changed = connection.execute(
                "UPDATE memory_thread_jobs SET lease_until = ? WHERE character_id = ? AND conversation_id = ? AND lease_token = ? AND leased_revision = ? AND lease_until > ? AND revision = ?",
                (
                    self._clock().timestamp() + self._lease_seconds,
                    lease.character_id,
                    str(lease.conversation_id),
                    str(lease.token),
                    lease.revision,
                    self._clock().timestamp(),
                    lease.revision,
                ),
            )
            return changed.rowcount == 1

    def snapshot(self, lease: ThreadLease) -> ThreadSnapshot:
        cutoff = self._clock() - self._retention
        with self._database.transaction() as connection:
            self._require_current(connection, lease)
            rows = connection.execute(
                f"SELECT {', '.join('t.' + column.strip() for column in TURN_COLUMNS.split(','))}, v.revision FROM conversation_turns t JOIN memory_turn_versions v ON v.character_id = t.character_id AND v.conversation_id = t.conversation_id AND v.turn_id = t.turn_id WHERE t.character_id = ? AND t.conversation_id = ? AND t.status = ? AND t.created_at >= ? AND NOT EXISTS (SELECT 1 FROM screen_turn_provenance p WHERE p.turn_id = t.turn_id) ORDER BY t.created_at, t.rowid",
                (
                    lease.character_id,
                    str(lease.conversation_id),
                    TurnStatus.COMPLETED.value,
                    format_datetime(cutoff),
                ),
            ).fetchall()
            return ThreadSnapshot(
                lease,
                tuple(
                    ThreadSource(turn_from_row(row), row["revision"]) for row in rows
                ),
                cutoff,
            )

    @contextmanager
    def guard(self, snapshot: ThreadSnapshot) -> Iterator[sqlite3.Connection]:
        # LLM処理はこの外で完了させる。会話書込を止めるのは保存に必要な短い区間だけ。
        with self._database.transaction() as connection:
            self._require_current(connection, snapshot.lease)
            now_cutoff = format_datetime(self._clock() - self._retention)
            for source in snapshot.sources:
                row = connection.execute(
                    "SELECT revision, deleted FROM memory_turn_versions WHERE character_id = ? AND conversation_id = ? AND turn_id = ?",
                    (
                        snapshot.lease.character_id,
                        str(snapshot.lease.conversation_id),
                        str(source.turn.turn_id),
                    ),
                ).fetchone()
                if (
                    row is None
                    or row["deleted"]
                    or row["revision"] != source.revision
                    or format_datetime(source.turn.created_at) < now_cutoff
                ):
                    raise StaleThreadSnapshot("thread source is no longer valid")
            yield connection

    def finish(
        self,
        connection: sqlite3.Connection,
        snapshot: ThreadSnapshot,
        *,
        saved_count: int,
        rejected_count: int,
    ) -> ThreadOutcome:
        self._require_current(connection, snapshot.lease)
        if any(type(n) is not int or n < 0 for n in (saved_count, rejected_count)):
            raise ValueError("result counts must be nonnegative integers")
        outcome = (
            ThreadOutcome.MIXED
            if saved_count and rejected_count
            else ThreadOutcome.SAVED
            if saved_count
            else ThreadOutcome.REJECTED
            if rejected_count
            else ThreadOutcome.EMPTY
        )
        lease = snapshot.lease
        connection.execute(
            "INSERT INTO memory_thread_receipts(character_id, conversation_id, revision, outcome, saved_count, rejected_count, completed_at) VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                lease.character_id,
                str(lease.conversation_id),
                lease.revision,
                outcome.value,
                saved_count,
                rejected_count,
                format_datetime(self._clock()),
            ),
        )
        connection.execute(
            "UPDATE memory_thread_jobs SET completed_revision = ?, lease_token = NULL, leased_revision = NULL, lease_until = NULL, next_attempt_at = 0, attempts = 0, last_outcome = ? WHERE character_id = ? AND conversation_id = ? AND lease_token = ?",
            (
                lease.revision,
                outcome.value,
                lease.character_id,
                str(lease.conversation_id),
                str(lease.token),
            ),
        )
        return outcome

    def release(self, lease: ThreadLease, *, failed: bool) -> None:
        # 後続更新がある場合は失敗backoffを引き継がず最新の予約を直ちに処理する。
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE memory_thread_jobs SET lease_token = NULL, leased_revision = NULL, lease_until = NULL, next_attempt_at = CASE WHEN revision = ? THEN ? ELSE 0 END, last_outcome = ? WHERE character_id = ? AND conversation_id = ? AND lease_token = ?",
                (
                    lease.revision,
                    self._clock().timestamp() + self._retry_seconds if failed else 0,
                    "FAILED" if failed else "STALE",
                    lease.character_id,
                    str(lease.conversation_id),
                    str(lease.token),
                ),
            )

    def has_pending(self) -> bool:
        with self._database.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM memory_thread_jobs WHERE revision > completed_revision LIMIT 1"
                ).fetchone()
                is not None
            )

    def _require_current(
        self, connection: sqlite3.Connection, lease: ThreadLease
    ) -> None:
        row = connection.execute(
            "SELECT revision, leased_revision, lease_token, lease_until FROM memory_thread_jobs WHERE character_id = ? AND conversation_id = ?",
            (lease.character_id, str(lease.conversation_id)),
        ).fetchone()
        if (
            row is None
            or row["revision"] != lease.revision
            or row["leased_revision"] != lease.revision
            or row["lease_token"] != str(lease.token)
            or row["lease_until"] <= self._clock().timestamp()
        ):
            raise StaleThreadSnapshot("thread revision or lease changed")

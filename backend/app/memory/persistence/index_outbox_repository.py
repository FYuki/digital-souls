from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.memory.persistence.sqlite import (
    ConnectionFactory,
    PersonaMemorySqlite,
    format_datetime,
)


Clock = Callable[[], datetime]


@dataclass(frozen=True)
class IndexOutboxEntry:
    id: str
    memory_id: str
    character_id: str
    operation: str
    attempt_count: int


class IndexOutboxRepository:
    def __init__(
        self,
        *,
        database_path: Path,
        clock: Clock,
        connection_factory: ConnectionFactory = sqlite3.connect,
    ) -> None:
        self._clock = clock
        self._database = PersonaMemorySqlite(database_path, connection_factory)

    def list_processable(
        self, *, limit: int, attempt_limit: int
    ) -> list[IndexOutboxEntry]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT id, memory_id, character_id, operation, attempt_count "
                "FROM memory_index_outbox "
                "WHERE status IN ('PENDING', 'FAILED') AND attempt_count < ? "
                "ORDER BY created_at, rowid LIMIT ?",
                (attempt_limit, limit),
            ).fetchall()
        return [
            IndexOutboxEntry(
                id=str(row["id"]),
                memory_id=str(row["memory_id"]),
                character_id=str(row["character_id"]),
                operation=str(row["operation"]),
                attempt_count=int(row["attempt_count"]),
            )
            for row in rows
        ]

    def mark_completed(self, *, outbox_id: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE memory_index_outbox SET status = 'COMPLETED', "
                "last_error_code = NULL, updated_at = ? WHERE id = ?",
                (format_datetime(self._now()), outbox_id),
            )

    def mark_failed(self, *, outbox_id: str, error_code: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE memory_index_outbox SET status = 'FAILED', "
                "attempt_count = attempt_count + 1, last_error_code = ?, "
                "updated_at = ? WHERE id = ?",
                (error_code, format_datetime(self._now()), outbox_id),
            )

    def list_incomplete_operations_by_memory(
        self, *, character_id: str
    ) -> dict[str, set[str]]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT memory_id, operation FROM memory_index_outbox "
                "WHERE character_id = ? AND status IN ('PENDING', 'FAILED')",
                (character_id,),
            ).fetchall()
        operations_by_memory: dict[str, set[str]] = {}
        for row in rows:
            operations_by_memory.setdefault(str(row["memory_id"]), set()).add(
                str(row["operation"])
            )
        return operations_by_memory

    def mark_memory_operation_completed(
        self, *, character_id: str, memory_id: str, operation: str
    ) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE memory_index_outbox SET status = 'COMPLETED', "
                "last_error_code = NULL, updated_at = ? "
                "WHERE character_id = ? AND memory_id = ? "
                "AND operation = ? AND status IN ('PENDING', 'FAILED')",
                (format_datetime(self._now()), character_id, memory_id, operation),
            )

    def list_character_ids(self) -> set[str]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT character_id FROM memory_index_outbox"
            ).fetchall()
        return {str(row["character_id"]) for row in rows}

    def status_counts(self) -> tuple[int, int]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM memory_index_outbox "
                "WHERE status IN ('PENDING', 'FAILED') GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return counts.get("PENDING", 0), counts.get("FAILED", 0)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

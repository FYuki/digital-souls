import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID

from app.conversation_history._sqlite import (
    ConnectionFactory,
    SqliteSession,
    format_datetime,
)

WAL_CLEANUP_FAILURE_REASON = "WAL_CHECKPOINT_FAILED"


class ConversationWalCleanup:
    def __init__(
        self,
        *,
        database_path: Path,
        clock: Callable[[], datetime],
        connection_factory: ConnectionFactory,
    ) -> None:
        self._clock = clock
        self._database = SqliteSession(database_path, connection_factory)

    def after_hard_delete(self, character_id: str, conversation_id: UUID) -> None:
        if not self._checkpoint_and_clear(character_id, conversation_id):
            self._record_initial_failure(character_id, conversation_id)

    def retry_pending(self) -> None:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT character_id, conversation_id FROM wal_cleanup_jobs "
                "ORDER BY created_at, character_id, conversation_id"
            ).fetchall()
        for row in rows:
            character_id = str(row[0])
            conversation_id = UUID(str(row[1]))
            if not self._checkpoint_and_clear(character_id, conversation_id):
                self._record_retry_failure(character_id, conversation_id)

    def _checkpoint_and_clear(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> bool:
        try:
            with self._database.connection() as connection:
                result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result is None or int(result[0]) != 0:
                raise sqlite3.OperationalError("WAL checkpoint was busy")
        except sqlite3.Error:
            return False
        with self._database.transaction() as connection:
            connection.execute(
                "DELETE FROM wal_cleanup_jobs WHERE character_id = ? "
                "AND conversation_id = ?",
                (character_id, str(conversation_id)),
            )
        return True

    def _record_initial_failure(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO wal_cleanup_jobs "
                "(character_id, conversation_id, reason_code, created_at, "
                "attempt_count) VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(character_id, conversation_id) DO UPDATE SET "
                "attempt_count = wal_cleanup_jobs.attempt_count + 1",
                (
                    character_id,
                    str(conversation_id),
                    WAL_CLEANUP_FAILURE_REASON,
                    format_datetime(self._clock()),
                ),
            )

    def _record_retry_failure(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE wal_cleanup_jobs SET attempt_count = attempt_count + 1 "
                "WHERE character_id = ? AND conversation_id = ?",
                (character_id, str(conversation_id)),
            )

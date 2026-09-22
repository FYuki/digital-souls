from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.conversation_history.sqlite_lease import (
    SQLiteLease,
    acquire_runtime_lease,
)
from app.sqlite_session import ConnectionFactory, SqliteSession


@contextmanager
def _runtime_access(database_path: Path) -> Iterator[SQLiteLease]:
    from app.restore_intent import require_sqlite_available

    with acquire_runtime_lease(database_path) as lease:
        require_sqlite_available(database_path)
        yield lease


class PersonaMemorySqlite(SqliteSession):
    """persona memory向けSQLite session。runtime leaseとWAL modeを強制する。"""

    def __init__(
        self,
        database_path: Path,
        connection_factory: ConnectionFactory,
    ) -> None:
        super().__init__(
            database_path,
            connection_factory,
            access=_runtime_access,
            pragmas=("journal_mode = WAL",),
        )

    def truncate_wal(
        self, *, busy_error: str = "persona memory WAL checkpoint was busy"
    ) -> None:
        super().truncate_wal(busy_error=busy_error)

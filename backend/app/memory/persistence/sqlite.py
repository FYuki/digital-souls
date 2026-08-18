from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.conversation_history.sqlite_lease import acquire_runtime_lease


ConnectionFactory = Callable[[Path], sqlite3.Connection]
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class PersonaMemorySqlite:
    def __init__(
        self,
        database_path: Path,
        connection_factory: ConnectionFactory,
    ) -> None:
        self._database_path = database_path
        self._connection_factory = connection_factory

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with acquire_runtime_lease(self._database_path):
            connection = self._connection_factory(self._database_path)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA secure_delete = ON")
                connection.execute("PRAGMA journal_mode = WAL")
                yield connection
            finally:
                connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def truncate_wal(self) -> None:
        with self.connection() as connection:
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or int(result[0]) != 0:
            raise sqlite3.OperationalError("persona memory WAL checkpoint was busy")


def format_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).strftime(DATETIME_FORMAT)


def parse_datetime(value: str) -> datetime:
    return datetime.strptime(value, DATETIME_FORMAT).replace(tzinfo=UTC)

"""SQLite接続・トランザクション・永続化時刻表現の共通基盤。

domainごとの差異（lease取得方法やWAL設定）は注入で切り替え、
接続・PRAGMA適用・トランザクション規約だけをここに集約する。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path


ConnectionFactory = Callable[[Path], sqlite3.Connection]
SqliteAccess = Callable[[Path], AbstractContextManager[object]]
"""接続単位でlease等のaccess制御を行うcontextmanager factory。"""

DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class SqliteSession:
    """PRAGMA適用とBEGIN IMMEDIATEトランザクションを統一するSQLite基盤。"""

    def __init__(
        self,
        database_path: Path,
        connection_factory: ConnectionFactory,
        *,
        access: SqliteAccess,
        pragmas: tuple[str, ...] = (),
    ) -> None:
        self._database_path = database_path
        self._connection_factory = connection_factory
        self._access = access
        self._pragmas = pragmas

    @property
    def database_path(self) -> Path:
        return self._database_path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self._access(self._database_path):
            connection = self._connection_factory(self._database_path)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA secure_delete = ON")
                for pragma in self._pragmas:
                    connection.execute(f"PRAGMA {pragma}")
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

    def truncate_wal(self, *, busy_error: str) -> None:
        """WAL checkpointを強制し、競合時はbusy_errorのOperationalErrorを送出する。"""
        with self.connection() as connection:
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or int(result[0]) != 0:
            raise sqlite3.OperationalError(busy_error)


def format_datetime(value: datetime) -> str:
    """timezone-awareなdatetimeをSQLite永続化用のUTC文字列へ変換する。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).strftime(DATETIME_FORMAT)


def parse_datetime(value: str) -> datetime:
    """format_datetimeで書き込まれた文字列をawareなUTC datetimeへ戻す。"""
    return datetime.strptime(value, DATETIME_FORMAT).replace(tzinfo=UTC)

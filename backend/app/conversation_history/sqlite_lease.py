from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, LOCK_UN, flock
from pathlib import Path
from typing import IO, Iterator, Literal


SQLITE_LEASE_FILENAME_SUFFIX = ".conversation-history.lease"


class SQLiteLeaseUnavailableError(RuntimeError):
    """別processが会話履歴SQLiteを使用している。"""


@dataclass
class SQLiteLease:
    database_path: Path
    _file: IO[str]
    mode: Literal["maintenance", "runtime"]

    def transition_to_runtime(self) -> None:
        if self.mode != "maintenance":
            raise RuntimeError("SQLite lease is not in maintenance mode")
        flock(self._file.fileno(), LOCK_SH | LOCK_NB)
        self.mode = "runtime"

    def require_maintenance_for(self, database_path: Path) -> None:
        if self.mode != "maintenance" or self.database_path != database_path:
            raise RuntimeError("matching SQLite maintenance lease is required")

    def close(self) -> None:
        flock(self._file.fileno(), LOCK_UN)
        self._file.close()


_CURRENT_SQLITE_LEASE: ContextVar[SQLiteLease | None] = ContextVar(
    "current_sqlite_lease", default=None
)


def _lease_path(database_path: Path) -> Path:
    return database_path.parent / SQLITE_LEASE_FILENAME_SUFFIX


def ensure_sqlite_lease_file(database_path: Path) -> None:
    descriptor = os.open(
        _lease_path(database_path),
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    os.close(descriptor)


def _acquire(
    database_path: Path, mode: Literal["maintenance", "runtime"]
) -> SQLiteLease:
    lease_path = _lease_path(database_path)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        lease_path,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        lease_file = os.fdopen(descriptor, "a", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise
    operation = LOCK_EX if mode == "maintenance" else LOCK_SH
    try:
        flock(lease_file.fileno(), operation | LOCK_NB)
    except BlockingIOError as error:
        lease_file.close()
        raise SQLiteLeaseUnavailableError("SQLite lease is unavailable") from error
    return SQLiteLease(database_path, lease_file, mode)


@contextmanager
def acquire_maintenance_lease(database_path: Path) -> Iterator[SQLiteLease]:
    lease = _acquire(database_path, "maintenance")
    token = _CURRENT_SQLITE_LEASE.set(lease)
    try:
        yield lease
    finally:
        _CURRENT_SQLITE_LEASE.reset(token)
        lease.close()


@contextmanager
def acquire_runtime_lease(database_path: Path) -> Iterator[SQLiteLease]:
    lease = _acquire(database_path, "runtime")
    token = _CURRENT_SQLITE_LEASE.set(lease)
    try:
        yield lease
    finally:
        _CURRENT_SQLITE_LEASE.reset(token)
        lease.close()


@contextmanager
def normal_sqlite_access(database_path: Path) -> Iterator[SQLiteLease]:
    from app.restore_intent import require_sqlite_available

    current = _CURRENT_SQLITE_LEASE.get()
    if current is not None and current.database_path == database_path:
        require_sqlite_available(database_path)
        yield current
        return
    try:
        with acquire_runtime_lease(database_path) as lease:
            require_sqlite_available(database_path)
            yield lease
    except SQLiteLeaseUnavailableError:
        require_sqlite_available(database_path)
        raise

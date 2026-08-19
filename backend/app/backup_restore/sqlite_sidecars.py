from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path

from app.backup_restore.models import RestoreSafetyError


SQLITE_WAL_SUFFIX = "-wal"
SQLITE_SHM_SUFFIX = "-shm"
SQLITE_JOURNAL_SUFFIX = "-journal"
SQLITE_WAL_HEADER_SIZE = 32
SQLITE_WAL_FRAME_HEADER_SIZE = 24
SQLITE_WAL_MAGIC_NUMBERS = {b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"}


def validate_sqlite_sidecars_for_restore(database: Path) -> None:
    wal = _sidecar(database, SQLITE_WAL_SUFFIX)
    shm = _sidecar(database, SQLITE_SHM_SUFFIX)
    journal = _sidecar(database, SQLITE_JOURNAL_SUFFIX)
    existing = tuple(os.path.lexists(path) for path in (wal, shm, journal))
    if existing == (False, False, False):
        return
    wal_exists, shm_exists, journal_exists = existing
    if not wal_exists or journal_exists:
        raise RestoreSafetyError("restore destination SQLite sidecar state is uncertain")
    wal_status = wal.lstat()
    shm_status = shm.lstat() if shm_exists else None
    if _has_open_sqlite_descriptor(database):
        raise RestoreSafetyError("restore destination SQLite sidecar state is uncertain")
    if not stat.S_ISREG(wal_status.st_mode) or (
        shm_status is not None and not stat.S_ISREG(shm_status.st_mode)
    ):
        raise RestoreSafetyError("restore destination SQLite sidecar state is uncertain")
    if wal_status.st_size == 0:
        return
    _validate_wal_shape(wal, wal_status.st_size)
    _verify_on_scratch(database)


def remove_replaced_sqlite_sidecars(database: Path) -> None:
    for suffix in (SQLITE_WAL_SUFFIX, SQLITE_SHM_SUFFIX, SQLITE_JOURNAL_SUFFIX):
        _sidecar(database, suffix).unlink(missing_ok=True)


def _verify_on_scratch(database: Path) -> None:
    with tempfile.TemporaryDirectory(
        prefix="sqlite-sidecar-verification-", dir=_scratch_parent(database)
    ) as temporary_directory:
        scratch = Path(temporary_directory) / database.name
        if scratch.resolve().is_relative_to(database.parent.resolve()):
            raise RestoreSafetyError(
                "restore destination SQLite verification area is unsafe"
            )
        for suffix in ("", SQLITE_WAL_SUFFIX):
            shutil.copyfile(_sidecar(database, suffix), _sidecar(scratch, suffix))
        _checkpoint(scratch)
        scratch_wal = _sidecar(scratch, SQLITE_WAL_SUFFIX)
        if scratch_wal.exists() and scratch_wal.stat().st_size != 0:
            raise RestoreSafetyError("restore destination SQLite state is uncertain")


def _checkpoint(database: Path) -> None:
    try:
        with closing(sqlite3.connect(database)) as connection:
            recovery = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as error:
        raise RestoreSafetyError(
            "restore destination SQLite sidecar state is uncertain"
        ) from error
    if (
        recovery is None
        or recovery[0] != 0
        or recovery[1] < 0
        or recovery[2] < 0
        or recovery[2] != recovery[1]
        or result is None
        or result[0] != 0
        or integrity != ("ok",)
    ):
        raise RestoreSafetyError("restore destination SQLite sidecar state is uncertain")


def _scratch_parent(database: Path) -> Path:
    data_root = database.parent.resolve()
    candidates = (Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp"))
    for candidate in dict.fromkeys(candidates):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if (
            resolved.is_dir()
            and not resolved.is_relative_to(data_root)
            and os.access(resolved, os.W_OK | os.X_OK)
        ):
            return resolved
    raise RestoreSafetyError("restore destination SQLite verification area is unsafe")


def _validate_wal_shape(wal: Path, wal_size: int) -> None:
    try:
        with wal.open("rb") as wal_file:
            header = wal_file.read(SQLITE_WAL_HEADER_SIZE)
    except OSError as error:
        raise RestoreSafetyError(
            "restore destination SQLite sidecar state is uncertain"
        ) from error
    if len(header) != SQLITE_WAL_HEADER_SIZE or header[:4] not in SQLITE_WAL_MAGIC_NUMBERS:
        raise RestoreSafetyError("restore destination SQLite sidecar state is uncertain")
    page_size = int.from_bytes(header[8:12], "big")
    if page_size == 1:
        page_size = 65536
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise RestoreSafetyError("restore destination SQLite sidecar state is uncertain")
    frame_size = SQLITE_WAL_FRAME_HEADER_SIZE + page_size
    if (wal_size - SQLITE_WAL_HEADER_SIZE) % frame_size:
        raise RestoreSafetyError("restore destination SQLite sidecar state is uncertain")


def _sidecar(database: Path, suffix: str) -> Path:
    return database.with_name(database.name + suffix)


def _has_open_sqlite_descriptor(database: Path) -> bool:
    sqlite_paths = {
        database.resolve(),
        _sidecar(database, SQLITE_WAL_SUFFIX).resolve(),
        _sidecar(database, SQLITE_SHM_SUFFIX).resolve(),
    }
    try:
        for descriptor in Path("/proc/self/fd").iterdir():
            try:
                if descriptor.resolve() in sqlite_paths:
                    return True
            except FileNotFoundError:
                continue
    except OSError:
        return True
    return False

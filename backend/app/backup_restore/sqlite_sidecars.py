from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from app.backup_restore.models import RestoreSafetyError


SQLITE_WAL_SUFFIX = "-wal"
SQLITE_SHM_SUFFIX = "-shm"
SQLITE_JOURNAL_SUFFIX = "-journal"


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
    if (
        wal.is_symlink()
        or (shm_exists and shm.is_symlink())
        or _has_open_sqlite_descriptor(database)
    ):
        raise RestoreSafetyError("restore destination SQLite sidecar state is uncertain")
    _verify_on_scratch(database)


def remove_replaced_sqlite_sidecars(database: Path) -> None:
    for suffix in (SQLITE_WAL_SUFFIX, SQLITE_SHM_SUFFIX, SQLITE_JOURNAL_SUFFIX):
        _sidecar(database, suffix).unlink(missing_ok=True)


def _verify_on_scratch(database: Path) -> None:
    with tempfile.TemporaryDirectory(
        prefix="sqlite-sidecar-verification-", dir=database.parent
    ) as temporary_directory:
        scratch = Path(temporary_directory) / database.name
        for suffix in ("", SQLITE_WAL_SUFFIX):
            shutil.copyfile(_sidecar(database, suffix), _sidecar(scratch, suffix))
        source_shm = _sidecar(database, SQLITE_SHM_SUFFIX)
        if source_shm.exists():
            shutil.copyfile(source_shm, _sidecar(scratch, SQLITE_SHM_SUFFIX))
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
        or recovery[1] <= 0
        or recovery[2] != recovery[1]
        or result is None
        or result[0] != 0
        or integrity != ("ok",)
    ):
        raise RestoreSafetyError("restore destination SQLite sidecar state is uncertain")


def _sidecar(database: Path, suffix: str) -> Path:
    return database.with_name(database.name + suffix)


def _has_open_sqlite_descriptor(database: Path) -> bool:
    sqlite_paths = {
        database.resolve(),
        _sidecar(database, SQLITE_WAL_SUFFIX).resolve(),
        _sidecar(database, SQLITE_SHM_SUFFIX).resolve(),
    }
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            if descriptor.resolve() in sqlite_paths:
                return True
        except FileNotFoundError:
            continue
    return False

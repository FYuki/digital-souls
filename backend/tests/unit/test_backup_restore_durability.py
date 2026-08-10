from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.backup_restore_test_support import (
    FIXED_BACKUP_TIME,
    FIXED_COMMIT,
    TEST_AUTHENTICATION_KEY,
    create_history_database,
    initialized_runtime,
)


def _closed_backup_generation(tmp_path: Path, repository_root: Path):
    from app.backup_restore import create_backup

    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    generation = create_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_root=tmp_path / "backups",
        retention_count=3,
        authentication_key=TEST_AUTHENTICATION_KEY,
        git_commit=FIXED_COMMIT,
        created_at=FIXED_BACKUP_TIME,
    )
    connection.close()
    return paths, generation


def _fsync_target(file_descriptor: int) -> str:
    mode = os.fstat(file_descriptor).st_mode
    if stat.S_ISDIR(mode):
        return "directory"
    target = Path(f"/proc/self/fd/{file_descriptor}").resolve()
    if target.name == "conversation-history.db":
        return "artifact"
    if target.suffix == ".json":
        return "json"
    return "staging"


def test_rst_safe_01_does_not_replace_database_when_staging_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths, generation = _closed_backup_generation(tmp_path, repository_root)
    original_database = paths.sqlite_path.read_bytes()
    replace = Mock(wraps=os.replace)

    def fail_fsync(_file_descriptor: int) -> None:
        raise OSError("injected staging fsync failure")

    monkeypatch.setattr(service.os, "fsync", fail_fsync)
    monkeypatch.setattr(service.os, "replace", replace)

    with pytest.raises(RestoreSafetyError):
        restore_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    replace.assert_not_called()
    assert paths.sqlite_path.read_bytes() == original_database


def test_rst_safe_01_persists_staging_then_replaces_then_persists_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths, generation = _closed_backup_generation(tmp_path, repository_root)
    events: list[str] = []
    original_replace = os.replace

    def record_fsync(file_descriptor: int) -> None:
        target = _fsync_target(file_descriptor)
        events.append("data-root-fsync" if target == "directory" else "staging-fsync")

    def record_replace(source: str | bytes | Path, destination: str | bytes | Path) -> None:
        events.append("replace")
        original_replace(source, destination)

    monkeypatch.setattr(service.os, "fsync", record_fsync)
    monkeypatch.setattr(service.os, "replace", record_replace)

    restore_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )

    assert events == ["staging-fsync", "replace", "data-root-fsync"]


def test_bkp_verify_01_persists_generation_before_and_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import create_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=True)
    events: list[str] = []
    original_rename = Path.rename

    def record_fsync(file_descriptor: int) -> None:
        target = _fsync_target(file_descriptor)
        events.append(f"{target}-fsync")

    def record_rename(source: Path, destination: Path) -> Path:
        events.append("publish")
        return original_rename(source, destination)

    monkeypatch.setattr(service.os, "fsync", record_fsync)
    monkeypatch.setattr(Path, "rename", record_rename)
    try:
        create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=tmp_path / "backups",
            retention_count=3,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )
    finally:
        connection.close()

    assert events == [
        "artifact-fsync",
        "json-fsync",
        "json-fsync",
        "directory-fsync",
        "publish",
        "directory-fsync",
    ]

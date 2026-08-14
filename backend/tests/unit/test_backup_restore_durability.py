from __future__ import annotations

import os
import shutil
import sqlite3
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

SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")


def test_rst_safe_01_snapshots_broken_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    from app.backup_restore import service

    database = tmp_path / "conversation-history.db"
    database.symlink_to(tmp_path / "missing-database")

    snapshot = service._sqlite_asset_snapshot(database)

    assert snapshot[""] is not None


def test_bkp_cleanup_01_preserves_primary_error_when_staging_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.backup_restore import BackupSchemaError, create_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    primary_error = BackupSchemaError("injected schema failure")
    monkeypatch.setattr(
        service,
        "verify_sqlite_database",
        Mock(side_effect=primary_error),
    )
    monkeypatch.setattr(
        service.shutil,
        "rmtree",
        Mock(side_effect=OSError("injected cleanup failure")),
    )

    try:
        with pytest.raises(BackupSchemaError) as captured:
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

    assert captured.value is primary_error


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


def _sqlite_snapshot(database: Path) -> dict[str, bytes | None]:
    return {
        suffix: path.read_bytes() if path.exists() else None
        for suffix in SQLITE_SUFFIXES
        for path in (database.with_name(database.name + suffix),)
    }


def _place_valid_wal_sidecars(
    tmp_path: Path, repository_root: Path, destination: Path
) -> sqlite3.Connection:
    source = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="wal-destination"
    )
    connection = create_history_database(source, wal=True)
    for suffix in ("", "-wal", "-shm"):
        source_file = source.sqlite_path.with_name(source.sqlite_path.name + suffix)
        destination_file = destination.with_name(destination.name + suffix)
        shutil.copyfile(source_file, destination_file)
    return connection


def _place_valid_wal_without_shm(
    tmp_path: Path, repository_root: Path, destination: Path
) -> sqlite3.Connection:
    source = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="wal-only-destination"
    )
    connection = create_history_database(source, wal=True)
    for suffix in ("", "-wal"):
        source_file = source.sqlite_path.with_name(source.sqlite_path.name + suffix)
        destination_file = destination.with_name(destination.name + suffix)
        shutil.copyfile(source_file, destination_file)
    return connection


def test_rst_safe_01_does_not_replace_database_when_staging_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths, generation = _closed_backup_generation(tmp_path, repository_root)
    connection = _place_valid_wal_sidecars(
        tmp_path, repository_root, paths.sqlite_path
    )
    sqlite_before = _sqlite_snapshot(paths.sqlite_path)
    replace = Mock(wraps=os.replace)

    def fail_fsync(_file_descriptor: int) -> None:
        raise OSError("injected staging fsync failure")

    monkeypatch.setattr(service.os, "fsync", fail_fsync)
    monkeypatch.setattr(service.os, "replace", replace)

    try:
        with pytest.raises(RestoreSafetyError):
            restore_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )
    finally:
        connection.close()

    replace.assert_not_called()
    assert _sqlite_snapshot(paths.sqlite_path) == sqlite_before


def test_rst_safe_01_preserves_all_sqlite_assets_when_scratch_checkpoint_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import (
        RestoreDurabilityUncertainError,
        RestoreSafetyError,
        restore_backup,
    )
    from app.backup_restore import service, sqlite_sidecars

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths, generation = _closed_backup_generation(tmp_path, repository_root)
    connection = _place_valid_wal_sidecars(
        tmp_path, repository_root, paths.sqlite_path
    )
    sqlite_before = _sqlite_snapshot(paths.sqlite_path)
    replace = Mock(wraps=os.replace)
    checkpoint = Mock(side_effect=OSError("injected scratch checkpoint failure"))

    monkeypatch.setattr(sqlite_sidecars, "_checkpoint", checkpoint)
    monkeypatch.setattr(service.os, "replace", replace)
    try:
        with pytest.raises(RestoreSafetyError) as raised:
            restore_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )
    finally:
        connection.close()

    assert not isinstance(raised.value, RestoreDurabilityUncertainError)
    checkpoint.assert_called_once()
    scratch_database = checkpoint.call_args.args[0]
    assert scratch_database != paths.sqlite_path
    assert not scratch_database.is_relative_to(paths.data_root)
    assert not scratch_database.parent.exists()
    replace.assert_not_called()
    assert _sqlite_snapshot(paths.sqlite_path) == sqlite_before


def test_rst_safe_01_rejects_restore_without_external_scratch_area(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.backup_restore import service, sqlite_sidecars

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths, generation = _closed_backup_generation(tmp_path, repository_root)
    connection = _place_valid_wal_sidecars(
        tmp_path, repository_root, paths.sqlite_path
    )
    sqlite_before = _sqlite_snapshot(paths.sqlite_path)
    replace = Mock(wraps=os.replace)

    monkeypatch.setattr(
        sqlite_sidecars.os,
        "access",
        lambda path, _mode: Path(path).resolve() == paths.data_root.resolve(),
    )
    monkeypatch.setattr(service.os, "replace", replace)
    try:
        with pytest.raises(RestoreSafetyError):
            restore_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )
    finally:
        connection.close()

    replace.assert_not_called()
    assert _sqlite_snapshot(paths.sqlite_path) == sqlite_before


def test_rst_safe_01_preserves_all_sqlite_assets_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths, generation = _closed_backup_generation(tmp_path, repository_root)
    connection = _place_valid_wal_sidecars(
        tmp_path, repository_root, paths.sqlite_path
    )
    sqlite_before = _sqlite_snapshot(paths.sqlite_path)

    replace = Mock(side_effect=OSError("injected replace failure"))
    monkeypatch.setattr(service.os, "replace", replace)
    try:
        with pytest.raises(RestoreSafetyError):
            restore_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )
    finally:
        connection.close()

    replace.assert_called_once()
    assert _sqlite_snapshot(paths.sqlite_path) == sqlite_before


def test_rst_safe_01_compares_rollback_assets_without_reading_entire_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths, generation = _closed_backup_generation(tmp_path, repository_root)
    original_read_bytes = Path.read_bytes

    def reject_sqlite_asset_read_bytes(path: Path) -> bytes:
        if path.name == "conversation-history.db" or path.name.startswith(
            "conversation-history.db-"
        ):
            raise AssertionError("SQLite assets must be compared incrementally")
        return original_read_bytes(path)

    replace = Mock(side_effect=OSError("injected replace failure"))
    monkeypatch.setattr(Path, "read_bytes", reject_sqlite_asset_read_bytes)
    monkeypatch.setattr(service.os, "replace", replace)

    with pytest.raises(RestoreSafetyError):
        restore_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    replace.assert_called_once()


def test_dur_uncertain_01_detects_database_changed_before_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreDurabilityUncertainError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths, generation = _closed_backup_generation(tmp_path, repository_root)
    original_database = paths.sqlite_path.read_bytes()

    def change_destination_then_fail(source: Path, destination: Path) -> None:
        shutil.copyfile(source, destination)
        raise OSError("injected failure after destination changed")

    monkeypatch.setattr(service.os, "replace", change_destination_then_fail)

    with pytest.raises(RestoreDurabilityUncertainError):
        restore_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    assert paths.sqlite_path.read_bytes() != original_database


def test_rst_safe_01_preserves_wal_without_shm_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths, generation = _closed_backup_generation(tmp_path, repository_root)
    connection = _place_valid_wal_without_shm(
        tmp_path, repository_root, paths.sqlite_path
    )
    sqlite_before = _sqlite_snapshot(paths.sqlite_path)

    replace = Mock(side_effect=OSError("injected replace failure"))
    monkeypatch.setattr(service.os, "replace", replace)
    try:
        with pytest.raises(RestoreSafetyError):
            restore_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )
    finally:
        connection.close()

    replace.assert_called_once()
    assert _sqlite_snapshot(paths.sqlite_path) == sqlite_before


def test_dur_uncertain_01_classifies_post_replace_sidecar_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreDurabilityUncertainError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths, generation = _closed_backup_generation(tmp_path, repository_root)

    def fail_cleanup(_database: Path) -> None:
        raise OSError("injected sidecar cleanup failure")

    monkeypatch.setattr(service, "remove_replaced_sqlite_sidecars", fail_cleanup)
    with pytest.raises(RestoreDurabilityUncertainError):
        restore_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    assert paths.sqlite_path.read_bytes() == (
        generation / "conversation-history.db"
    ).read_bytes()


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

    assert events == [
        "staging-fsync",
        "staging-fsync",
        "data-root-fsync",
        "replace",
        "data-root-fsync",
        "data-root-fsync",
    ]


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


def test_dur_uncertain_01_keeps_replaced_database_when_data_root_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.backup_restore as backup_restore
    from app.backup_restore import service

    assert hasattr(backup_restore, "RestoreDurabilityUncertainError"), (
        "DUR-UNCERTAIN-01 requires a post-replace uncertainty error"
    )
    RestoreDurabilityUncertainError = (
        backup_restore.RestoreDurabilityUncertainError
    )
    restore_backup = backup_restore.restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths, generation = _closed_backup_generation(tmp_path, repository_root)
    artifact = generation / "conversation-history.db"
    expected_database = artifact.read_bytes()
    paths.sqlite_path.write_bytes(b"old destination")
    replace = Mock(wraps=os.replace)
    original_fsync_directory = service._fsync_directory

    def fail_data_root_fsync(path: Path) -> None:
        if path == paths.data_root:
            raise OSError("injected data root fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(service, "_fsync_directory", fail_data_root_fsync)
    monkeypatch.setattr(service.os, "replace", replace)

    with pytest.raises(RestoreDurabilityUncertainError):
        restore_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    replace.assert_called_once()
    assert paths.sqlite_path.read_bytes() == expected_database
    assert not tuple(paths.data_root.glob(".conversation-history.db.staging-*"))


def test_dur_uncertain_01_keeps_published_generation_and_skips_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.backup_restore as backup_restore
    from app.backup_restore import service

    assert hasattr(backup_restore, "BackupPublicationUncertainError"), (
        "DUR-UNCERTAIN-01 requires a post-publication uncertainty error"
    )
    BackupPublicationUncertainError = (
        backup_restore.BackupPublicationUncertainError
    )
    create_backup = backup_restore.create_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    backup_root = tmp_path / "backups"
    published = False
    original_rename = Path.rename
    original_fsync_directory = service._fsync_directory

    def record_publish(source: Path, destination: Path) -> Path:
        nonlocal published
        result = original_rename(source, destination)
        published = True
        return result

    def fail_publication_fsync(path: Path) -> None:
        if path == backup_root and published:
            raise OSError("injected publication fsync failure")
        original_fsync_directory(path)

    prune = Mock()
    monkeypatch.setattr(Path, "rename", record_publish)
    monkeypatch.setattr(service, "_fsync_directory", fail_publication_fsync)
    monkeypatch.setattr(service, "_prune_backup_generations", prune)
    try:
        with pytest.raises(BackupPublicationUncertainError):
            create_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_root=backup_root,
                retention_count=1,
                authentication_key=TEST_AUTHENTICATION_KEY,
                git_commit=FIXED_COMMIT,
                created_at=FIXED_BACKUP_TIME,
            )
    finally:
        connection.close()

    prune.assert_not_called()
    generations = tuple(backup_root.glob("backup-*"))
    assert len(generations) == 1
    assert (generations[0] / "conversation-history.db").is_file()
    assert not tuple(backup_root.glob(".backup-staging-*"))


def test_should_fail_creation_and_skip_retention_when_published_generation_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import BackupArtifactError, create_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    backup_root = tmp_path / "backups"
    existing_generations = tuple(
        create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=8,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME.replace(day=index + 1),
        )
        for index in range(8)
    )
    fsync_directory = service._fsync_directory
    prune = Mock(wraps=service._prune_backup_generations)
    published_generation: Path | None = None

    def invalidate_after_publication(path: Path) -> None:
        nonlocal published_generation
        fsync_directory(path)
        if path != backup_root or published_generation is not None:
            return
        published_generation = next(
            generation
            for generation in backup_root.glob("backup-*")
            if generation not in existing_generations
        )
        with (published_generation / "conversation-history.db").open("ab") as artifact:
            artifact.write(b"invalid-after-publication")

    monkeypatch.setattr(service, "_fsync_directory", invalidate_after_publication)
    monkeypatch.setattr(service, "_prune_backup_generations", prune)
    try:
        with pytest.raises(BackupArtifactError):
            create_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_root=backup_root,
                retention_count=7,
                authentication_key=TEST_AUTHENTICATION_KEY,
                git_commit=FIXED_COMMIT,
                created_at=FIXED_BACKUP_TIME.replace(day=9),
            )
    finally:
        connection.close()

    prune.assert_not_called()
    assert all(generation.is_dir() for generation in existing_generations)
    assert published_generation is not None
    assert published_generation.is_dir()
    assert set(backup_root.glob("backup-*")) == {
        *existing_generations,
        published_generation,
    }


def test_dur_uncertain_01_does_not_classify_prepublication_failure_as_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.backup_restore as backup_restore
    from app.backup_restore import service

    assert hasattr(backup_restore, "BackupPublicationUncertainError"), (
        "DUR-UNCERTAIN-01 requires a post-publication uncertainty error"
    )
    BackupPublicationUncertainError = (
        backup_restore.BackupPublicationUncertainError
    )
    create_backup = backup_restore.create_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    backup_root = tmp_path / "backups"
    original_fsync_directory = service._fsync_directory

    def fail_staging_directory_fsync(path: Path) -> None:
        if path.name.startswith(".backup-staging-"):
            raise OSError("injected staging directory fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(service, "_fsync_directory", fail_staging_directory_fsync)
    try:
        with pytest.raises(OSError) as captured:
            create_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_root=backup_root,
                retention_count=1,
                authentication_key=TEST_AUTHENTICATION_KEY,
                git_commit=FIXED_COMMIT,
                created_at=FIXED_BACKUP_TIME,
            )
    finally:
        connection.close()

    assert not isinstance(captured.value, BackupPublicationUncertainError)
    assert not tuple(backup_root.glob("backup-*"))

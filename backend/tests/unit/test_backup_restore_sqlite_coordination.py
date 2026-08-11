from __future__ import annotations

import importlib.util
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

import pytest

from tests.backup_restore_test_support import (
    FIXED_BACKUP_TIME,
    FIXED_COMMIT,
    TEST_AUTHENTICATION_KEY,
    create_history_database,
    initialized_runtime,
)
from app.runtime_paths import RuntimePaths


SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _require_sqlite_lease_contract() -> None:
    assert importlib.util.find_spec("app.conversation_history.sqlite_lease") is not None, (
        "SQLITE-LEASE-01 requires the conversation-history lease module"
    )


def _create_generation(
    tmp_path: Path, repository_root: Path
) -> tuple[RuntimePaths, Path]:
    from app.backup_restore import create_backup

    source_paths = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="source"
    )
    connection = create_history_database(source_paths, wal=False)
    try:
        generation = create_backup(
            runtime_paths=source_paths,
            repository_root=repository_root,
            backup_root=tmp_path / "backups",
            retention_count=3,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )
    finally:
        connection.close()
    return source_paths, generation


def _sqlite_snapshot(database: Path) -> dict[str, bytes | None]:
    return {
        suffix: (
            path.read_bytes() if path.exists() else None
        )
        for suffix in SQLITE_SUFFIXES
        for path in (database.with_name(database.name + suffix),)
    }


def _directory_snapshot(directory: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(directory)): path.read_bytes() if path.is_file() else None
        for path in sorted(directory.rglob("*"))
    }


def test_sqlite_lease_01_rejects_restore_without_modifying_files_while_runtime_holds_lease(
    tmp_path: Path,
) -> None:
    _require_sqlite_lease_contract()
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.conversation_history.sqlite_lease import acquire_runtime_lease

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source_paths, generation = _create_generation(tmp_path, repository_root)
    destination = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="destination"
    )
    connection = create_history_database(destination, wal=False)
    connection.close()
    for suffix in ("-wal", "-shm", "-journal"):
        destination.sqlite_path.with_name(
            destination.sqlite_path.name + suffix
        ).write_bytes(f"runtime-owned:{suffix}".encode())
    sqlite_before = _sqlite_snapshot(destination.sqlite_path)
    generation_before = _directory_snapshot(generation)
    backup_root_before = _directory_snapshot(generation.parent)
    with acquire_runtime_lease(destination.sqlite_path):
        started_at = monotonic()
        with pytest.raises(RestoreSafetyError):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )
        elapsed = monotonic() - started_at

    assert elapsed < 1.0
    assert _sqlite_snapshot(destination.sqlite_path) == sqlite_before
    assert _directory_snapshot(generation) == generation_before
    assert _directory_snapshot(generation.parent) == backup_root_before


def test_sqlite_lease_01_does_not_process_sidecars_before_maintenance_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_sqlite_lease_contract()
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.backup_restore import service as backup_restore_service
    from app.conversation_history.sqlite_lease import acquire_runtime_lease

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source_paths, generation = _create_generation(tmp_path, repository_root)
    destination = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="destination"
    )
    wal_source = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="wal-source"
    )
    connection = create_history_database(wal_source, wal=True)
    for suffix in ("", "-wal", "-shm"):
        source = wal_source.sqlite_path.with_name(wal_source.sqlite_path.name + suffix)
        target = destination.sqlite_path.with_name(destination.sqlite_path.name + suffix)
        shutil.copyfile(source, target)
    before = _sqlite_snapshot(destination.sqlite_path)
    sidecar_processing_count = 0

    def record_sidecar_processing(database: Path) -> None:
        nonlocal sidecar_processing_count
        sidecar_processing_count += 1

    monkeypatch.setattr(
        backup_restore_service,
        "validate_sqlite_sidecars_for_restore",
        record_sidecar_processing,
    )
    try:
        with acquire_runtime_lease(destination.sqlite_path):
            with pytest.raises(RestoreSafetyError):
                restore_backup(
                    runtime_paths=destination,
                    repository_root=repository_root,
                    backup_directory=generation,
                    authentication_key=TEST_AUTHENTICATION_KEY,
                )
    finally:
        connection.close()

    assert sidecar_processing_count == 0
    assert _sqlite_snapshot(destination.sqlite_path) == before


def test_sqlite_lease_01_does_not_modify_any_sqlite_file_when_maintenance_is_unavailable(
    tmp_path: Path,
) -> None:
    _require_sqlite_lease_contract()
    from app.conversation_history.sqlite_lease import (
        acquire_maintenance_lease,
        acquire_runtime_lease,
    )

    database = tmp_path / "conversation-history.db"
    for suffix in SQLITE_SUFFIXES:
        database.with_name(database.name + suffix).write_bytes(
            f"content:{suffix}".encode()
        )
    before = _sqlite_snapshot(database)

    with acquire_runtime_lease(database):
        with pytest.raises(RuntimeError):
            with acquire_maintenance_lease(database):
                pytest.fail("maintenance lease must not be acquired")

    assert _sqlite_snapshot(database) == before


def test_sqlite_lease_01_rejects_runtime_before_sqlite_is_opened_during_maintenance(
    tmp_path: Path,
) -> None:
    _require_sqlite_lease_contract()
    from app.conversation_history.sqlite_lease import (
        acquire_maintenance_lease,
        acquire_runtime_lease,
    )

    database = tmp_path / "conversation-history.db"
    sqlite_opened = False

    with acquire_maintenance_lease(database):
        with pytest.raises(RuntimeError):
            with acquire_runtime_lease(database):
                sqlite_opened = True

    assert sqlite_opened is False
    runtime_acquired_after_release = False
    with acquire_runtime_lease(database):
        runtime_acquired_after_release = True
    assert runtime_acquired_after_release is True


def test_sqlite_lease_01_blocks_runtime_acquisition_during_sidecar_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_sqlite_lease_contract()
    from app.backup_restore import restore_backup
    from app.backup_restore import service as backup_restore_service
    from app.conversation_history.sqlite_lease import acquire_runtime_lease

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source_paths, generation = _create_generation(tmp_path, repository_root)
    destination = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="destination"
    )
    wal_source = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="wal-source"
    )
    connection = create_history_database(wal_source, wal=True)
    for suffix in ("", "-wal", "-shm"):
        source = wal_source.sqlite_path.with_name(wal_source.sqlite_path.name + suffix)
        target = destination.sqlite_path.with_name(destination.sqlite_path.name + suffix)
        shutil.copyfile(source, target)
    validate_sqlite_sidecars = (
        backup_restore_service.validate_sqlite_sidecars_for_restore
    )
    runtime_rejected_during_validation = False

    def observe_lease_boundary(database: Path) -> None:
        nonlocal runtime_rejected_during_validation
        with pytest.raises(RuntimeError):
            with acquire_runtime_lease(database):
                pytest.fail("runtime lease must not be acquired during validation")
        runtime_rejected_during_validation = True
        validate_sqlite_sidecars(database)

    monkeypatch.setattr(
        backup_restore_service,
        "validate_sqlite_sidecars_for_restore",
        observe_lease_boundary,
    )
    try:
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )
    finally:
        connection.close()

    assert runtime_rejected_during_validation is True


def test_sqlite_sidecar_01_restores_after_converging_quiesced_valid_wal(
    tmp_path: Path,
) -> None:
    from app.backup_restore import restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source_paths, generation = _create_generation(tmp_path, repository_root)
    destination = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="destination"
    )
    wal_source = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="wal-source"
    )
    connection = create_history_database(wal_source, wal=True)
    assert wal_source.sqlite_path.with_name(
        wal_source.sqlite_path.name + "-wal"
    ).is_file()
    for suffix in ("", "-wal", "-shm"):
        source = wal_source.sqlite_path.with_name(wal_source.sqlite_path.name + suffix)
        target = destination.sqlite_path.with_name(destination.sqlite_path.name + suffix)
        shutil.copyfile(source, target)
    connection.close()

    result = restore_backup(
        runtime_paths=destination,
        repository_root=repository_root,
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )

    assert result.conversation_count == 1
    assert not destination.sqlite_path.with_name(
        destination.sqlite_path.name + "-wal"
    ).exists()
    assert not destination.sqlite_path.with_name(
        destination.sqlite_path.name + "-shm"
    ).exists()


def test_sqlite_sidecar_01_restores_valid_wal_without_shm(
    tmp_path: Path,
) -> None:
    from app.backup_restore import restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source_paths, generation = _create_generation(tmp_path, repository_root)
    destination = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="destination"
    )
    wal_source = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="wal-only-source"
    )
    connection = create_history_database(wal_source, wal=True)
    for suffix in ("", "-wal"):
        source = wal_source.sqlite_path.with_name(wal_source.sqlite_path.name + suffix)
        target = destination.sqlite_path.with_name(destination.sqlite_path.name + suffix)
        shutil.copyfile(source, target)
    before = _sqlite_snapshot(destination.sqlite_path)
    assert before["-wal"] is not None
    assert before["-shm"] is None

    try:
        result = restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )
    finally:
        connection.close()

    assert result.conversation_count == 1
    assert not destination.sqlite_path.with_name(
        destination.sqlite_path.name + "-wal"
    ).exists()
    assert not destination.sqlite_path.with_name(
        destination.sqlite_path.name + "-shm"
    ).exists()


@pytest.mark.parametrize("suffix", ("-wal", "-shm", "-journal"))
def test_sqlite_sidecar_01_preserves_database_and_malformed_sidecars(
    tmp_path: Path,
    suffix: str,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source_paths, generation = _create_generation(tmp_path, repository_root)
    destination = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="destination"
    )
    connection = create_history_database(destination, wal=False)
    connection.close()
    sidecar = destination.sqlite_path.with_name(destination.sqlite_path.name + suffix)
    sidecar.write_bytes(b"malformed sqlite sidecar")
    before = _sqlite_snapshot(destination.sqlite_path)

    with pytest.raises(RestoreSafetyError):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    assert _sqlite_snapshot(destination.sqlite_path) == before


def test_sqlite_sidecar_01_preserves_database_with_foreign_valid_wal(
    tmp_path: Path,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source_paths, generation = _create_generation(tmp_path, repository_root)
    destination = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="destination"
    )
    destination_connection = create_history_database(destination, wal=False)
    destination_connection.close()
    foreign = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="foreign"
    )
    with closing(sqlite3.connect(foreign.sqlite_path)) as setup_connection:
        setup_connection.execute("PRAGMA page_size = 8192")
        setup_connection.execute("VACUUM")
        setup_connection.execute("CREATE TABLE foreign_data (value TEXT NOT NULL)")
    foreign_connection = sqlite3.connect(foreign.sqlite_path)
    assert (
        foreign_connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    )
    foreign_connection.execute("PRAGMA wal_autocheckpoint = 0")
    foreign_connection.execute("INSERT INTO foreign_data VALUES ('foreign')")
    foreign_connection.commit()
    foreign_wal = foreign.sqlite_path.with_name(foreign.sqlite_path.name + "-wal")
    destination_wal = destination.sqlite_path.with_name(
        destination.sqlite_path.name + "-wal"
    )
    shutil.copyfile(foreign_wal, destination_wal)
    assert destination_wal.read_bytes()[:4] in {
        b"\x37\x7f\x06\x82",
        b"\x37\x7f\x06\x83",
    }
    before = _sqlite_snapshot(destination.sqlite_path)

    try:
        with pytest.raises(RestoreSafetyError):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )
    finally:
        foreign_connection.close()

    assert _sqlite_snapshot(destination.sqlite_path) == before

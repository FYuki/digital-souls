from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from time import monotonic
from unittest.mock import Mock

import pytest

from app.runtime_paths import RESTORE_INTENT_FILENAME, RuntimePaths
from tests.backup_restore_test_support import (
    FIXED_BACKUP_TIME,
    FIXED_COMMIT,
    TEST_AUTHENTICATION_KEY,
    create_history_database,
    initialized_runtime,
)


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


def test_restore_startup_block_01_rechecks_marker_with_runtime_lease_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreRecoveryRequiredError
    from app.conversation_history._sqlite import SqliteSession
    from app.conversation_history.sqlite_lease import acquire_maintenance_lease
    from app import restore_intent

    database = tmp_path / "conversation-history.db"
    marker = tmp_path / RESTORE_INTENT_FILENAME
    connection_factory = Mock(side_effect=AssertionError("SQLite must stay closed"))
    require_sqlite_available = restore_intent.require_sqlite_available
    check_count = 0

    def persist_marker_after_runtime_lease(database_path: Path) -> None:
        nonlocal check_count
        check_count += 1
        if check_count == 1:
            with pytest.raises(RuntimeError):
                with acquire_maintenance_lease(database_path):
                    pytest.fail("maintenance lease must be excluded")
            marker.write_bytes(b"restore recovery required")
        require_sqlite_available(database_path)

    session = SqliteSession(database, connection_factory)
    monkeypatch.setattr(
        restore_intent,
        "require_sqlite_available",
        persist_marker_after_runtime_lease,
    )

    with pytest.raises(RestoreRecoveryRequiredError):
        with session.connection():
            pytest.fail("SQLite connection must not be yielded")

    assert check_count == 1
    connection_factory.assert_not_called()


def test_restore_startup_block_01_does_not_open_after_concurrent_restore_marks_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreRecoveryRequiredError
    from app.conversation_history import schema
    from app.conversation_history.sqlite_lease import acquire_maintenance_lease

    database = tmp_path / "conversation-history.db"
    marker = tmp_path / RESTORE_INTENT_FILENAME
    sqlite_connect = Mock(side_effect=AssertionError("SQLite must stay closed"))
    monkeypatch.setattr(schema.sqlite3, "connect", sqlite_connect)

    with acquire_maintenance_lease(database):
        marker.write_bytes(b"restore recovery required")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                schema.inspect_conversation_history_schema,
                database,
            )
            with pytest.raises(RestoreRecoveryRequiredError):
                future.result()

    sqlite_connect.assert_not_called()


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import restore_backup
    from app.backup_restore import sqlite_sidecars

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
    checkpoint = Mock(wraps=sqlite_sidecars._checkpoint)
    monkeypatch.setattr(sqlite_sidecars, "_checkpoint", checkpoint)
    monkeypatch.setattr(tempfile, "tempdir", str(destination.data_root))

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
    checkpoint.assert_called_once()
    scratch_database = checkpoint.call_args.args[0]
    assert not scratch_database.is_relative_to(destination.data_root)
    assert not scratch_database.parent.exists()


def test_sqlite_sidecar_01_accepts_empty_checkpoint_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.backup_restore import sqlite_sidecars

    database = tmp_path / "conversation-history.db"
    connection = Mock()
    connection.execute.side_effect = (
        Mock(fetchone=Mock(return_value=(0, 0, 0))),
        Mock(fetchone=Mock(return_value=(0, 0, 0))),
        Mock(fetchone=Mock(return_value=("ok",))),
    )
    monkeypatch.setattr(sqlite_sidecars.sqlite3, "connect", Mock(return_value=connection))

    sqlite_sidecars._checkpoint(database)

    connection.close.assert_called_once_with()


@pytest.mark.parametrize("checkpoint", ((1, 1, 1), (0, 2, 1), (0, -1, -1)))
def test_sqlite_sidecar_01_rejects_unsafe_checkpoint_result(
    checkpoint: tuple[int, int, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreSafetyError, sqlite_sidecars

    connection = Mock()
    connection.execute.side_effect = (
        Mock(fetchone=Mock(return_value=checkpoint)),
        Mock(fetchone=Mock(return_value=(0, 0, 0))),
        Mock(fetchone=Mock(return_value=("ok",))),
    )
    monkeypatch.setattr(sqlite_sidecars.sqlite3, "connect", Mock(return_value=connection))

    with pytest.raises(RestoreSafetyError):
        sqlite_sidecars._checkpoint(tmp_path / "conversation-history.db")


def test_sqlite_lease_01_closes_descriptor_when_fdopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.conversation_history import sqlite_lease

    opened_descriptors: list[int] = []

    def fail_fdopen(descriptor: int, *_args: object, **_kwargs: object) -> None:
        opened_descriptors.append(descriptor)
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(sqlite_lease.os, "fdopen", fail_fdopen)

    with pytest.raises(OSError, match="injected fdopen failure"):
        with sqlite_lease.acquire_maintenance_lease(
            tmp_path / "conversation-history.db"
        ):
            pytest.fail("lease acquisition must fail")

    assert len(opened_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(opened_descriptors[0])


def test_sqlite_lease_01_closes_file_when_flock_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.conversation_history import sqlite_lease

    opened_files = []
    original_fdopen = sqlite_lease.os.fdopen

    def record_fdopen(*args, **kwargs):
        lease_file = original_fdopen(*args, **kwargs)
        opened_files.append(lease_file)
        return lease_file

    monkeypatch.setattr(sqlite_lease.os, "fdopen", record_fdopen)
    monkeypatch.setattr(
        sqlite_lease,
        "flock",
        Mock(side_effect=OSError("injected flock failure")),
    )

    with pytest.raises(OSError, match="injected flock failure"):
        with sqlite_lease.acquire_maintenance_lease(
            tmp_path / "conversation-history.db"
        ):
            pytest.fail("lease acquisition must fail")

    assert len(opened_files) == 1
    assert opened_files[0].closed


def test_sqlite_lease_01_closes_file_when_unlock_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.conversation_history import sqlite_lease

    lease_path = tmp_path / "lease"
    lease_file = lease_path.open("w", encoding="utf-8")
    lease = sqlite_lease.SQLiteLease(tmp_path / "database", lease_file, "maintenance")
    monkeypatch.setattr(
        sqlite_lease,
        "flock",
        Mock(side_effect=OSError("injected unlock failure")),
    )

    with pytest.raises(OSError, match="injected unlock failure"):
        lease.close()

    assert lease_file.closed


def test_sqlite_session_01_closes_connection_when_pragma_setup_fails(
    tmp_path: Path,
) -> None:
    from app.conversation_history._sqlite import SqliteSession

    connection = Mock()
    connection.execute.side_effect = sqlite3.OperationalError("injected pragma failure")
    session = SqliteSession(tmp_path / "conversation-history.db", Mock(return_value=connection))

    with pytest.raises(sqlite3.OperationalError, match="injected pragma failure"):
        with session.connection():
            pytest.fail("connection setup must fail")

    connection.close.assert_called_once_with()


def test_sqlite_sidecar_01_treats_unavailable_proc_as_in_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.backup_restore import sqlite_sidecars

    original_iterdir = Path.iterdir

    def fail_proc_iterdir(path: Path):
        if path == Path("/proc/self/fd"):
            raise FileNotFoundError("injected missing proc")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_proc_iterdir)

    assert sqlite_sidecars._has_open_sqlite_descriptor(
        tmp_path / "conversation-history.db"
    )


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


def test_sqlite_sidecar_01_accepts_an_empty_wal_for_restore(tmp_path: Path) -> None:
    from app.backup_restore.sqlite_sidecars import validate_sqlite_sidecars_for_restore

    database = tmp_path / "conversation-history.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.commit()
    wal = database.with_name(database.name + "-wal")
    wal.touch()

    validate_sqlite_sidecars_for_restore(database)

    assert database.is_file()
    assert not wal.exists() or wal.stat().st_size == 0


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
@pytest.mark.parametrize("sidecar_type", ("fifo", "directory"))
def test_sqlite_sidecar_01_rejects_non_regular_sidecar(
    tmp_path: Path,
    suffix: str,
    sidecar_type: str,
) -> None:
    from app.backup_restore import RestoreSafetyError
    from app.backup_restore.sqlite_sidecars import validate_sqlite_sidecars_for_restore

    database = tmp_path / "conversation-history.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.commit()
    wal = database.with_name(database.name + "-wal")
    wal.touch()
    sidecar = database.with_name(database.name + suffix)
    if sidecar_type == "fifo":
        if sidecar == wal:
            wal.unlink()
        os.mkfifo(sidecar)
    else:
        if sidecar == wal:
            wal.unlink()
        sidecar.mkdir()

    with pytest.raises(RestoreSafetyError):
        validate_sqlite_sidecars_for_restore(database)

    assert database.is_file()
    assert sidecar.exists()


def test_sqlite_sidecar_01_restores_with_an_empty_regular_wal(tmp_path: Path) -> None:
    from app.backup_restore import restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source_paths, generation = _create_generation(tmp_path, repository_root)
    destination = initialized_runtime(
        tmp_path, repository_root, environment_id="test", name="destination"
    )
    connection = create_history_database(destination, wal=False)
    connection.close()
    wal = destination.sqlite_path.with_name(destination.sqlite_path.name + "-wal")
    shm = destination.sqlite_path.with_name(destination.sqlite_path.name + "-shm")
    wal.touch()

    result = restore_backup(
        runtime_paths=destination,
        repository_root=repository_root,
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )

    assert result.conversation_count == 1
    assert not wal.exists()
    assert not shm.exists()


@pytest.mark.parametrize("operation", ["ensure", "acquire"])
def test_sqlite_lease_01_rejects_symlink_without_changing_target(
    operation: str,
    tmp_path: Path,
) -> None:
    from app.conversation_history.sqlite_lease import (
        SQLITE_LEASE_FILENAME_SUFFIX,
        acquire_maintenance_lease,
        ensure_sqlite_lease_file,
    )

    database = tmp_path / "conversation-history.db"
    target = tmp_path / "external-lease-target"
    target.write_text("keep", encoding="utf-8")
    lease_path = tmp_path / SQLITE_LEASE_FILENAME_SUFFIX
    lease_path.symlink_to(target)

    with pytest.raises(OSError):
        if operation == "ensure":
            ensure_sqlite_lease_file(database)
        else:
            with acquire_maintenance_lease(database):
                pytest.fail("symlinked lease must not be acquired")

    assert lease_path.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"


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

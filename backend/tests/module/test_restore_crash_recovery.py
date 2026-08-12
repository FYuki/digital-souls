from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI

from app.runtime_paths import RuntimePaths
from tests.backup_restore_test_support import (
    FIXED_BACKUP_TIME,
    FIXED_COMMIT,
    TEST_AUTHENTICATION_KEY,
    create_history_database,
    initialized_runtime,
)


RESTORE_INTENT_FILENAME = ".conversation-history.restore-intent.json"
SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")
SECOND_CONVERSATION_ID = "bb82c80b-d4fe-4d20-b860-9f3847b58bd1"
EXIT_BEFORE_REPLACE = 70
EXIT_AFTER_REPLACE = 71
EXIT_UNEXPECTED_FAILURE = 72
EXIT_AFTER_WAL_UNLINK = 73
EXIT_AFTER_SHM_UNLINK = 74
EXIT_AFTER_JOURNAL_UNLINK = 75
JOURNAL_SENTINEL = b"restore-cleanup-journal"


def _intent_path(paths: RuntimePaths) -> Path:
    return paths.data_root / RESTORE_INTENT_FILENAME


def _sqlite_snapshot(database: Path) -> dict[str, bytes | None]:
    return {
        suffix: path.read_bytes() if path.exists() else None
        for suffix in SQLITE_SUFFIXES
        for path in (database.with_name(database.name + suffix),)
    }


def _create_generation(
    tmp_path: Path, repository_root: Path
) -> tuple[RuntimePaths, Path]:
    from app.backup_restore import create_backup

    source = initialized_runtime(tmp_path, repository_root, name="source")
    connection = create_history_database(source, wal=False)
    try:
        generation = create_backup(
            runtime_paths=source,
            repository_root=repository_root,
            backup_root=tmp_path / "backups",
            retention_count=3,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )
    finally:
        connection.close()
    return source, generation


def _create_destination(
    tmp_path: Path, repository_root: Path, name: str
) -> RuntimePaths:
    destination = initialized_runtime(tmp_path, repository_root, name=name)
    connection = create_history_database(destination, wal=False)
    connection.close()
    return destination


def _create_later_generation(
    source: RuntimePaths, repository_root: Path, backup_root: Path
) -> Path:
    from app.backup_restore import create_backup

    return create_backup(
        runtime_paths=source,
        repository_root=repository_root,
        backup_root=backup_root,
        retention_count=3,
        authentication_key=TEST_AUTHENTICATION_KEY,
        git_commit=FIXED_COMMIT,
        created_at=FIXED_BACKUP_TIME,
    )


def _wait_for_child(process_id: int) -> int:
    waited_process_id, status = os.waitpid(process_id, 0)
    assert waited_process_id == process_id
    assert os.WIFEXITED(status)
    return os.WEXITSTATUS(status)


def _exit_restore_immediately_before_replace(
    destination: RuntimePaths,
    repository_root: Path,
    generation: Path,
) -> int:
    process_id = os.fork()
    if process_id != 0:
        return _wait_for_child(process_id)

    from app.backup_restore import restore_backup
    from app.backup_restore import service

    def exit_before_replace(
        _source: str | bytes | Path, _destination: str | bytes | Path
    ) -> None:
        os._exit(EXIT_BEFORE_REPLACE)

    service.os.replace = exit_before_replace
    try:
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )
    except BaseException:
        os._exit(EXIT_UNEXPECTED_FAILURE)
    os._exit(EXIT_UNEXPECTED_FAILURE)


def _exit_restore_immediately_after_replace(
    destination: RuntimePaths,
    repository_root: Path,
    generation: Path,
) -> int:
    process_id = os.fork()
    if process_id != 0:
        return _wait_for_child(process_id)

    from app.backup_restore import restore_backup
    from app.backup_restore import service

    original_replace = os.replace

    def exit_after_replace(
        source: str | bytes | Path, destination_path: str | bytes | Path
    ) -> None:
        original_replace(source, destination_path)
        os._exit(EXIT_AFTER_REPLACE)

    service.os.replace = exit_after_replace
    try:
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )
    except BaseException:
        os._exit(EXIT_UNEXPECTED_FAILURE)
    os._exit(EXIT_UNEXPECTED_FAILURE)


def _exit_restore_immediately_after_sidecar_unlink(
    destination: RuntimePaths,
    repository_root: Path,
    generation: Path,
    target_suffix: str,
    expected_exit_code: int,
) -> int:
    process_id = os.fork()
    if process_id != 0:
        return _wait_for_child(process_id)

    from app.backup_restore import restore_backup
    from app.backup_restore import service

    original_replace = os.replace
    original_unlink = Path.unlink
    target = destination.sqlite_path.with_name(
        destination.sqlite_path.name + target_suffix
    )

    def replace_and_create_journal(
        source: str | bytes | Path, destination_path: str | bytes | Path
    ) -> None:
        original_replace(source, destination_path)
        if Path(destination_path) == destination.sqlite_path:
            journal = destination.sqlite_path.with_name(
                destination.sqlite_path.name + "-journal"
            )
            journal.write_bytes(JOURNAL_SENTINEL)

    def exit_after_target_unlink(
        path: Path, *args: object, **kwargs: object
    ) -> None:
        if path == target and not path.is_file():
            os._exit(EXIT_UNEXPECTED_FAILURE)
        original_unlink(path, *args, **kwargs)
        if path == target:
            if path.exists():
                os._exit(EXIT_UNEXPECTED_FAILURE)
            os._exit(expected_exit_code)

    service.os.replace = replace_and_create_journal
    Path.unlink = exit_after_target_unlink
    try:
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )
    except BaseException:
        os._exit(EXIT_UNEXPECTED_FAILURE)
    os._exit(EXIT_UNEXPECTED_FAILURE)


def _leave_second_conversation_in_wal(database: Path) -> None:
    process_id = os.fork()
    if process_id != 0:
        assert _wait_for_child(process_id) == 0
        return

    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute(
        "INSERT INTO conversations (character_id, conversation_id, created_at) "
        "VALUES (?, ?, ?)",
        ("miori", SECOND_CONVERSATION_ID, "2026-08-08T02:00:00.000000Z"),
    )
    connection.commit()
    os._exit(0)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_backend_startup_is_blocked_before_sqlite_open(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.backup_restore as backup_restore
    from app import main

    assert hasattr(backup_restore, "RestoreRecoveryRequiredError")
    inspect_schema = Mock(side_effect=AssertionError("SQLite must stay closed"))
    sqlite_connect = Mock(side_effect=AssertionError("SQLite must stay closed"))

    async def start_backend() -> None:
        with pytest.raises(backup_restore.RestoreRecoveryRequiredError):
            async with main.lifespan(FastAPI()):
                pytest.fail("backend startup must remain blocked")

    with monkeypatch.context() as patch:
        patch.setattr(main, "resolve_runtime_paths", lambda *_args: paths)
        patch.setattr(main, "inspect_conversation_history_schema", inspect_schema)
        patch.setattr(main.sqlite3, "connect", sqlite_connect)
        asyncio.run(start_backend())

    inspect_schema.assert_not_called()
    sqlite_connect.assert_not_called()


def test_restore_crash_recovery_01_recovers_after_process_exit_between_intent_and_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(tmp_path, repository_root)
    destination = _create_destination(tmp_path, repository_root, "destination-a")
    sqlite_before = _sqlite_snapshot(destination.sqlite_path)

    exit_code = _exit_restore_immediately_before_replace(
        destination, repository_root, generation
    )

    assert exit_code == EXIT_BEFORE_REPLACE
    marker = _intent_path(destination)
    assert marker.is_file()
    assert marker.stat().st_mode & 0o777 == 0o600
    marker_value = json.loads(marker.read_text(encoding="utf-8"))
    assert set(marker_value) == {
        "formatVersion",
        "environmentId",
        "generationSequence",
        "artifactSha256",
        "generationIdentitySha256",
    }
    metadata = json.loads((generation / "metadata.json").read_text(encoding="utf-8"))
    expected_generation_identity = hashlib.sha256(
        json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert marker_value["generationIdentitySha256"] == expected_generation_identity
    assert _sqlite_snapshot(destination.sqlite_path) == sqlite_before
    _assert_backend_startup_is_blocked_before_sqlite_open(destination, monkeypatch)

    result = restore_backup(
        runtime_paths=destination,
        repository_root=repository_root,
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )

    assert result.conversation_count == 1
    assert not _intent_path(destination).exists()
    assert all(
        not destination.sqlite_path.with_name(
            destination.sqlite_path.name + suffix
        ).exists()
        for suffix in SQLITE_SUFFIXES[1:]
    )


def test_restore_crash_recovery_01_prevents_old_wal_application_after_process_exit_between_replace_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import restore_backup
    from app.backup_restore.sqlite_snapshot import verify_sqlite_database

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(tmp_path, repository_root)
    destination = _create_destination(tmp_path, repository_root, "destination-b")
    _leave_second_conversation_in_wal(destination.sqlite_path)
    wal = destination.sqlite_path.with_name(destination.sqlite_path.name + "-wal")
    old_wal = wal.read_bytes()
    artifact = generation / "conversation-history.db"

    exit_code = _exit_restore_immediately_after_replace(
        destination, repository_root, generation
    )

    assert exit_code == EXIT_AFTER_REPLACE
    assert destination.sqlite_path.read_bytes() == artifact.read_bytes()
    assert wal.read_bytes() == old_wal
    assert _intent_path(destination).is_file()
    _assert_backend_startup_is_blocked_before_sqlite_open(destination, monkeypatch)

    result = restore_backup(
        runtime_paths=destination,
        repository_root=repository_root,
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )

    assert result.conversation_count == 1
    assert verify_sqlite_database(destination.sqlite_path).conversation_count == 1
    assert _sha256(destination.sqlite_path) == _sha256(artifact)
    assert not _intent_path(destination).exists()
    assert all(
        not destination.sqlite_path.with_name(
            destination.sqlite_path.name + suffix
        ).exists()
        for suffix in SQLITE_SUFFIXES[1:]
    )


@pytest.mark.parametrize(
    ("target_suffix", "expected_exit_code"),
    (
        pytest.param("-wal", EXIT_AFTER_WAL_UNLINK, id="wal"),
        pytest.param("-shm", EXIT_AFTER_SHM_UNLINK, id="shm"),
        pytest.param("-journal", EXIT_AFTER_JOURNAL_UNLINK, id="journal"),
    ),
)
def test_restore_crash_recovery_01_should_recover_after_each_sidecar_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_suffix: str,
    expected_exit_code: int,
) -> None:
    from app.backup_restore import RestoreRecoveryRequiredError, restore_backup
    from app.backup_restore import service
    from app.backup_restore.sqlite_snapshot import verify_sqlite_database

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    source, generation = _create_generation(tmp_path, repository_root)
    different_generation = _create_later_generation(
        source, repository_root, tmp_path / "backups"
    )
    destination = _create_destination(tmp_path, repository_root, "destination-c")
    _leave_second_conversation_in_wal(destination.sqlite_path)
    before_cleanup = _sqlite_snapshot(destination.sqlite_path)
    assert before_cleanup["-wal"] is not None
    assert before_cleanup["-shm"] is not None

    exit_code = _exit_restore_immediately_after_sidecar_unlink(
        destination,
        repository_root,
        generation,
        target_suffix,
        expected_exit_code,
    )

    assert exit_code == expected_exit_code
    expected_partial = {
        **before_cleanup,
        "": (generation / "conversation-history.db").read_bytes(),
        "-journal": JOURNAL_SENTINEL,
    }
    target_index = SQLITE_SUFFIXES.index(target_suffix)
    for removed_suffix in SQLITE_SUFFIXES[1 : target_index + 1]:
        expected_partial[removed_suffix] = None
    assert _sqlite_snapshot(destination.sqlite_path) == expected_partial
    marker = _intent_path(destination)
    assert marker.is_file()
    marker_before_rejection = marker.read_bytes()
    sqlite_before_rejection = _sqlite_snapshot(destination.sqlite_path)

    with pytest.raises(RestoreRecoveryRequiredError):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=different_generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    assert _sqlite_snapshot(destination.sqlite_path) == sqlite_before_rejection
    assert marker.read_bytes() == marker_before_rejection

    original_replace = os.replace
    replaced_from_verified_artifact: list[bool] = []

    def record_replacement(
        staging: str | bytes | Path, destination_path: str | bytes | Path
    ) -> None:
        staging_path = Path(staging)
        if Path(destination_path) == destination.sqlite_path:
            replaced_from_verified_artifact.append(
                staging_path.read_bytes()
                == (generation / "conversation-history.db").read_bytes()
            )
        original_replace(staging, destination_path)

    monkeypatch.setattr(service.os, "replace", record_replacement)

    result = restore_backup(
        runtime_paths=destination,
        repository_root=repository_root,
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )

    assert replaced_from_verified_artifact == [True]
    assert result.conversation_count == 1
    assert verify_sqlite_database(destination.sqlite_path).conversation_count == 1
    assert _sha256(destination.sqlite_path) == _sha256(
        generation / "conversation-history.db"
    )
    assert not marker.exists()
    assert all(
        not destination.sqlite_path.with_name(
            destination.sqlite_path.name + suffix
        ).exists()
        for suffix in SQLITE_SUFFIXES[1:]
    )

from __future__ import annotations

import json
import hashlib
import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.runtime_paths import RuntimePaths
from tests.backup_restore_test_support import (
    FIXED_BACKUP_TIME,
    FIXED_COMMIT,
    TEST_AUTHENTICATION_KEY,
    create_history_database,
    initialized_runtime,
    read_json,
)


RESTORE_INTENT_FILENAME = ".conversation-history.restore-intent.json"
SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _intent_path(paths: RuntimePaths) -> Path:
    return paths.data_root / RESTORE_INTENT_FILENAME


def _sqlite_snapshot(database: Path) -> dict[str, bytes | None]:
    return {
        suffix: path.read_bytes() if path.exists() else None
        for suffix in SQLITE_SUFFIXES
        for path in (database.with_name(database.name + suffix),)
    }


def _marker_snapshot(marker: Path) -> tuple[bool, bool, bytes | str | None]:
    if not os.path.lexists(marker):
        return False, False, None
    if marker.is_symlink():
        return True, True, os.readlink(marker)
    return True, False, marker.read_bytes()


def _create_generation(
    tmp_path: Path, repository_root: Path, environment_id: str = "test"
) -> tuple[RuntimePaths, Path]:
    from app.backup_restore import create_backup

    source = initialized_runtime(
        tmp_path,
        repository_root,
        environment_id=environment_id,
        name=f"source-{environment_id}",
    )
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
    tmp_path: Path, repository_root: Path, name: str = "destination"
) -> RuntimePaths:
    destination = initialized_runtime(tmp_path, repository_root, name=name)
    connection = create_history_database(destination, wal=False)
    connection.close()
    return destination


def _valid_marker(paths: RuntimePaths, generation: Path) -> dict[str, object]:
    metadata = read_json(generation / "metadata.json")
    generation_identity_sha256 = hashlib.sha256(
        json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "formatVersion": 1,
        "environmentId": paths.environment_id,
        "generationSequence": metadata["generationSequence"],
        "artifactSha256": metadata["artifactSha256"],
        "generationIdentitySha256": generation_identity_sha256,
    }


def _write_marker(
    paths: RuntimePaths, generation: Path, overrides: dict[str, object] | None = None
) -> Path:
    marker = _valid_marker(paths, generation)
    if overrides is not None:
        marker = {**marker, **overrides}
    marker_path = _intent_path(paths)
    marker_path.write_text(
        json.dumps(marker, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    marker_path.chmod(0o600)
    return marker_path


def _recovery_error_type():
    import app.backup_restore as backup_restore

    assert hasattr(backup_restore, "RestoreRecoveryRequiredError"), (
        "RESTORE-INTENT-01 requires a typed recovery-required error"
    )
    return backup_restore.RestoreRecoveryRequiredError


def _assert_sqlite_session_remains_blocked(paths: RuntimePaths) -> None:
    from app.conversation_history._sqlite import SqliteSession

    connection_factory = Mock(side_effect=AssertionError("SQLite must stay closed"))
    with pytest.raises(_recovery_error_type()):
        SqliteSession(paths.sqlite_path, connection_factory)
    connection_factory.assert_not_called()


def test_restore_intent_01_exposes_dedicated_runtime_marker_path(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()

    paths = initialized_runtime(tmp_path, repository_root)

    assert paths.restore_intent_path == paths.data_root / RESTORE_INTENT_FILENAME
    assert paths.restore_intent_path not in {
        paths.sqlite_path,
        paths.identity_marker_path,
    }


def test_restore_intent_01_resumes_same_generation_idempotently(
    tmp_path: Path,
) -> None:
    from app.backup_restore import restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(tmp_path, repository_root)
    destination = _create_destination(tmp_path, repository_root)
    marker = _write_marker(destination, generation)

    result = restore_backup(
        runtime_paths=destination,
        repository_root=repository_root,
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )

    assert result.conversation_count == 1
    assert destination.sqlite_path.read_bytes() == (
        generation / "conversation-history.db"
    ).read_bytes()
    assert not marker.exists()
    assert all(
        not destination.sqlite_path.with_name(
            destination.sqlite_path.name + suffix
        ).exists()
        for suffix in SQLITE_SUFFIXES[1:]
    )


def test_restore_intent_01_rejects_different_generation_without_mutation(
    tmp_path: Path,
) -> None:
    from app.backup_restore import create_backup, restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    source, generation_a = _create_generation(tmp_path, repository_root)
    import sqlite3

    source_connection = sqlite3.connect(source.sqlite_path)
    try:
        generation_b = create_backup(
            runtime_paths=source,
            repository_root=repository_root,
            backup_root=tmp_path / "backups",
            retention_count=3,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )
    finally:
        source_connection.close()
    destination = _create_destination(tmp_path, repository_root)
    destination.sqlite_path.write_bytes(b"different-generation-destination")
    marker = _write_marker(destination, generation_a)
    sqlite_before = _sqlite_snapshot(destination.sqlite_path)
    marker_before = _marker_snapshot(marker)

    with pytest.raises(_recovery_error_type()):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation_b,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    assert _sqlite_snapshot(destination.sqlite_path) == sqlite_before
    assert _marker_snapshot(marker) == marker_before


def test_restore_intent_01_rejects_foreign_generation_without_mutation(
    tmp_path: Path,
) -> None:
    from app.backup_restore import restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(
        tmp_path, repository_root, environment_id="dev"
    )
    destination = _create_destination(tmp_path, repository_root)
    destination.sqlite_path.write_bytes(b"foreign-generation-destination")
    marker = _write_marker(destination, generation)
    sqlite_before = _sqlite_snapshot(destination.sqlite_path)
    marker_before = _marker_snapshot(marker)

    with pytest.raises(_recovery_error_type()):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    assert _sqlite_snapshot(destination.sqlite_path) == sqlite_before
    assert _marker_snapshot(marker) == marker_before


@pytest.mark.parametrize(
    "invalid_marker",
    (
        "malformed",
        "symlink",
        "environment-identity",
        "generation-identity",
        "artifact-checksum",
        "field-set",
        "format-version-type",
        "environment-id-type",
        "generation-sequence-type",
        "artifact-sha256-type",
        "generation-identity-sha256-type",
        "format-version-value",
        "generation-sequence-range",
        "artifact-sha256-format",
        "generation-identity-sha256-format",
    ),
)
def test_restore_intent_01_rejects_invalid_marker_without_mutation(
    tmp_path: Path,
    invalid_marker: str,
) -> None:
    from app.backup_restore import restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(tmp_path, repository_root)
    destination = _create_destination(tmp_path, repository_root)
    destination.sqlite_path.write_bytes(b"invalid-marker-destination")
    marker = _intent_path(destination)
    symlink_target: Path | None = None
    symlink_target_before: bytes | None = None
    if invalid_marker == "malformed":
        marker.write_bytes(b"{")
    elif invalid_marker == "symlink":
        symlink_target = tmp_path / "external-marker"
        symlink_target.write_text("do not follow", encoding="utf-8")
        symlink_target_before = symlink_target.read_bytes()
        marker.symlink_to(symlink_target)
    else:
        overrides = {
            "environment-identity": {"environmentId": "dev"},
            "generation-identity": {"generationIdentitySha256": "0" * 64},
            "artifact-checksum": {"artifactSha256": "0" * 64},
            "field-set": {"unexpected": "field"},
            "format-version-type": {"formatVersion": "1"},
            "environment-id-type": {"environmentId": 1},
            "generation-sequence-type": {"generationSequence": True},
            "artifact-sha256-type": {"artifactSha256": 0},
            "generation-identity-sha256-type": {
                "generationIdentitySha256": 0
            },
            "format-version-value": {"formatVersion": 2},
            "generation-sequence-range": {"generationSequence": 0},
            "artifact-sha256-format": {"artifactSha256": "z" * 64},
            "generation-identity-sha256-format": {
                "generationIdentitySha256": "z" * 64
            },
        }[invalid_marker]
        _write_marker(destination, generation, overrides)
    sqlite_before = _sqlite_snapshot(destination.sqlite_path)
    marker_before = _marker_snapshot(marker)

    with pytest.raises(_recovery_error_type()):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    assert _sqlite_snapshot(destination.sqlite_path) == sqlite_before
    assert _marker_snapshot(marker) == marker_before
    if symlink_target is not None:
        assert symlink_target.read_bytes() == symlink_target_before


@pytest.mark.parametrize("failure", ("file-fsync", "directory-fsync"))
def test_restore_intent_01_does_not_replace_when_marker_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from app.backup_restore import restore_backup
    from app.backup_restore.models import BackupError
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(tmp_path, repository_root)
    destination = _create_destination(tmp_path, repository_root)
    sqlite_before = _sqlite_snapshot(destination.sqlite_path)
    original_fsync = os.fsync
    replace = Mock(wraps=os.replace)

    def fail_marker_persistence(file_descriptor: int) -> None:
        mode = os.fstat(file_descriptor).st_mode
        if failure == "directory-fsync" and stat.S_ISDIR(mode):
            raise OSError("injected marker directory fsync failure")
        if not stat.S_ISDIR(mode):
            target = Path(f"/proc/self/fd/{file_descriptor}").resolve()
            if failure == "file-fsync" and target.name == RESTORE_INTENT_FILENAME:
                raise OSError("injected marker file fsync failure")
        original_fsync(file_descriptor)

    monkeypatch.setattr(service.os, "fsync", fail_marker_persistence)
    monkeypatch.setattr(service.os, "replace", replace)

    with pytest.raises(BackupError):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    replace.assert_not_called()
    assert _sqlite_snapshot(destination.sqlite_path) == sqlite_before


def test_restore_intent_01_does_not_replace_when_marker_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import restore_backup
    from app.backup_restore.models import BackupError
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(tmp_path, repository_root)
    destination = _create_destination(tmp_path, repository_root)
    sqlite_before = _sqlite_snapshot(destination.sqlite_path)
    original_open = os.open
    replace = Mock(wraps=os.replace)

    def fail_marker_creation(
        path: Path,
        flags: int,
        mode: int = 0o777,
    ) -> int:
        if path == destination.restore_intent_path:
            raise OSError("injected marker creation failure")
        return original_open(path, flags, mode)

    monkeypatch.setattr(service.os, "open", fail_marker_creation)
    monkeypatch.setattr(service.os, "replace", replace)

    with pytest.raises(BackupError):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    replace.assert_not_called()
    assert _sqlite_snapshot(destination.sqlite_path) == sqlite_before


def test_restore_crash_recovery_01_keeps_marker_when_commit_directory_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreDurabilityUncertainError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(tmp_path, repository_root)
    destination = _create_destination(tmp_path, repository_root)
    original_fsync = os.fsync
    directory_fsync_count = 0

    def fail_commit_fsync(file_descriptor: int) -> None:
        nonlocal directory_fsync_count
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            directory_fsync_count += 1
            if directory_fsync_count == 2:
                raise OSError("injected commit directory fsync failure")
        original_fsync(file_descriptor)

    monkeypatch.setattr(service.os, "fsync", fail_commit_fsync)

    with pytest.raises(RestoreDurabilityUncertainError):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    assert _intent_path(destination).is_file()
    _assert_sqlite_session_remains_blocked(destination)


def test_restore_crash_recovery_01_reestablishes_marker_when_final_directory_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreDurabilityUncertainError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(tmp_path, repository_root)
    destination = _create_destination(tmp_path, repository_root)
    original_fsync = os.fsync
    directory_fsync_count = 0

    def fail_final_fsync(file_descriptor: int) -> None:
        nonlocal directory_fsync_count
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            directory_fsync_count += 1
            if directory_fsync_count == 3:
                raise OSError("injected final directory fsync failure")
        original_fsync(file_descriptor)

    monkeypatch.setattr(service.os, "fsync", fail_final_fsync)

    with pytest.raises(RestoreDurabilityUncertainError):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    assert _intent_path(destination).is_file()
    _assert_sqlite_session_remains_blocked(destination)


def test_restore_intent_01_cleans_marker_safely_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(tmp_path, repository_root)
    destination = _create_destination(tmp_path, repository_root)
    sqlite_before = _sqlite_snapshot(destination.sqlite_path)
    marker_observed_before_replace = False

    def fail_replace(_source: object, _destination: object) -> None:
        nonlocal marker_observed_before_replace
        marker_observed_before_replace = _intent_path(destination).is_file()
        raise OSError("injected replace failure")

    monkeypatch.setattr(
        service.os,
        "replace",
        Mock(side_effect=fail_replace),
    )

    with pytest.raises(RestoreSafetyError):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    assert _sqlite_snapshot(destination.sqlite_path) == sqlite_before
    assert marker_observed_before_replace is True
    assert not _intent_path(destination).exists()
    assert not tuple(destination.data_root.glob(".conversation-history.db.staging-*"))


def test_restore_intent_01_keeps_marker_when_replace_failure_cleanup_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreDurabilityUncertainError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(tmp_path, repository_root)
    destination = _create_destination(tmp_path, repository_root)
    sqlite_before = _sqlite_snapshot(destination.sqlite_path)
    original_fsync = os.fsync
    directory_fsync_count = 0

    def fail_cleanup_fsync(file_descriptor: int) -> None:
        nonlocal directory_fsync_count
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            directory_fsync_count += 1
            if directory_fsync_count == 2:
                raise OSError("injected marker cleanup fsync failure")
        original_fsync(file_descriptor)

    monkeypatch.setattr(service.os, "fsync", fail_cleanup_fsync)
    monkeypatch.setattr(
        service.os,
        "replace",
        Mock(side_effect=OSError("injected replace failure")),
    )

    with pytest.raises(RestoreDurabilityUncertainError):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    assert _sqlite_snapshot(destination.sqlite_path) == sqlite_before
    assert _intent_path(destination).is_file()


def test_restore_crash_recovery_01_should_keep_existing_intent_when_replacement_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(tmp_path, repository_root)
    destination = _create_destination(tmp_path, repository_root)
    marker = _write_marker(destination, generation)
    sqlite_before = _sqlite_snapshot(destination.sqlite_path)
    marker_before = marker.read_bytes()
    remove_sidecars = Mock(
        side_effect=AssertionError("sidecar cleanup must follow replacement")
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            service.os,
            "replace",
            Mock(side_effect=OSError("injected recovery replace failure")),
        )
        patch.setattr(service, "remove_replaced_sqlite_sidecars", remove_sidecars)

        with pytest.raises(RestoreSafetyError):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )

    assert _sqlite_snapshot(destination.sqlite_path) == sqlite_before
    assert marker.read_bytes() == marker_before
    remove_sidecars.assert_not_called()

    result = restore_backup(
        runtime_paths=destination,
        repository_root=repository_root,
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )

    assert result.conversation_count == 1
    assert not marker.exists()


def test_restore_intent_01_persists_full_commit_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_generation(tmp_path, repository_root)
    destination = _create_destination(tmp_path, repository_root)
    marker = _intent_path(destination)
    events: list[str] = []
    original_fsync = os.fsync
    original_replace = os.replace
    original_unlink = Path.unlink
    original_remove_sidecars = service.remove_replaced_sqlite_sidecars
    original_verify = service.verify_sqlite_database

    def record_fsync(file_descriptor: int) -> None:
        mode = os.fstat(file_descriptor).st_mode
        if stat.S_ISDIR(mode):
            events.append("directory-fsync")
        else:
            target = Path(f"/proc/self/fd/{file_descriptor}").resolve()
            events.append(
                "marker-fsync"
                if target.name == RESTORE_INTENT_FILENAME
                else "staging-fsync"
            )
        original_fsync(file_descriptor)

    def record_replace(
        source: str | bytes | Path, destination_path: str | bytes | Path
    ) -> None:
        events.append("replace")
        original_replace(source, destination_path)

    def record_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == marker:
            events.append("marker-unlink")
        original_unlink(path, *args, **kwargs)

    def record_sidecar_cleanup(database: Path) -> None:
        events.append("sidecar-cleanup")
        original_remove_sidecars(database)

    def record_verification(database: Path):
        result = original_verify(database)
        if database == destination.sqlite_path:
            events.append("post-verify")
        return result

    monkeypatch.setattr(service.os, "fsync", record_fsync)
    monkeypatch.setattr(service.os, "replace", record_replace)
    monkeypatch.setattr(Path, "unlink", record_unlink)
    monkeypatch.setattr(
        service, "remove_replaced_sqlite_sidecars", record_sidecar_cleanup
    )
    monkeypatch.setattr(service, "verify_sqlite_database", record_verification)

    restore_backup(
        runtime_paths=destination,
        repository_root=repository_root,
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )

    assert events == [
        "staging-fsync",
        "marker-fsync",
        "directory-fsync",
        "replace",
        "sidecar-cleanup",
        "directory-fsync",
        "post-verify",
        "marker-unlink",
        "directory-fsync",
    ]

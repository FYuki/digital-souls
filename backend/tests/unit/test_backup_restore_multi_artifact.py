from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from app.conversation_history.schema import (
    CURRENT_TABLES as CONVERSATION_TABLES,
    SCHEMA_VERSION as CONVERSATION_SCHEMA_VERSION,
)
from app.memory.persistence.schema import (
    PERSONA_MEMORY_TABLES,
    SCHEMA_VERSION as PERSONA_SCHEMA_VERSION,
)
from app.runtime_paths import RuntimePaths
from tests.backup_restore_test_support import (
    CONVERSATION_SENTINEL,
    FIXED_BACKUP_TIME,
    FIXED_COMMIT,
    TEST_AUTHENTICATION_KEY,
    TURN_ID,
    create_history_database,
    create_persona_memory_database,
    database_projection,
    initialized_runtime,
    persona_memory_projection,
    read_json,
)


ARTIFACT_FILENAMES = ("conversation-history.db", "persona-memory.db")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inject_second_replace_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Path, Path], None]:
    real_replace = os.replace
    replace_count = 0

    def fail_second_replace(source: Path, target: Path) -> None:
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
            raise OSError("second replace failed")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second_replace)
    return real_replace


def _create_pair_generation(
    tmp_path: Path,
    repository_root: Path,
    *,
    with_persona_memory: bool = True,
) -> tuple[RuntimePaths, Path]:
    from app.backup_restore import create_backup

    paths = initialized_runtime(tmp_path, repository_root, name="source")
    history = create_history_database(paths, wal=False)
    if with_persona_memory:
        create_persona_memory_database(
            paths, repository_root, with_approved_memory=True
        )
    try:
        generation = create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=tmp_path / "backups",
            retention_count=3,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )
    finally:
        history.close()
    return paths, generation


def test_backup_creates_fixed_pair_and_initializes_missing_persona_database(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()

    paths, generation = _create_pair_generation(
        tmp_path, repository_root, with_persona_memory=False
    )
    metadata = read_json(generation / "metadata.json")
    manifest = read_json(generation / "manifest.json")
    raw_artifacts = metadata["artifacts"]
    raw_manifest_files = manifest["files"]
    assert isinstance(raw_artifacts, list)
    assert all(isinstance(artifact, dict) for artifact in raw_artifacts)
    assert isinstance(raw_manifest_files, list)
    assert all(isinstance(entry, dict) for entry in raw_manifest_files)
    artifacts = cast(list[dict[str, object]], raw_artifacts)
    manifest_files = cast(list[dict[str, object]], raw_manifest_files)

    assert paths.persona_memory_sqlite_path.is_file()
    assert metadata["formatVersion"] == 3
    assert [artifact["filename"] for artifact in artifacts] == list(
        ARTIFACT_FILENAMES
    )
    assert [entry["path"] for entry in manifest_files] == [
        *ARTIFACT_FILENAMES,
        "metadata.json",
    ]
    expected_artifact_values = (
        (
            CONVERSATION_SCHEMA_VERSION,
            sorted(CONVERSATION_TABLES),
            1,
        ),
        (
            PERSONA_SCHEMA_VERSION,
            sorted(PERSONA_MEMORY_TABLES),
            0,
        ),
    )
    for artifact, filename, expected in zip(
        artifacts,
        ARTIFACT_FILENAMES,
        expected_artifact_values,
        strict=True,
    ):
        schema_version, required_tables, record_count = expected
        assert artifact["sha256"] == _sha256(generation / filename)
        assert artifact["integrityCheck"] == "ok"
        assert artifact["schemaVersion"] == schema_version
        assert artifact["requiredTables"] == required_tables
        assert artifact["recordCount"] == record_count
    assert persona_memory_projection(generation / "persona-memory.db") == (1, 0)


def test_persona_artifact_cannot_be_reauthenticated_by_rewriting_unsigned_values(
    tmp_path: Path,
) -> None:
    from app.backup_restore import BackupArtifactError, verify_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _paths, generation = _create_pair_generation(tmp_path, repository_root)
    persona_path = generation / "persona-memory.db"
    persona_path.write_bytes(persona_path.read_bytes() + b"tampered")

    metadata_path = generation / "metadata.json"
    metadata = read_json(metadata_path)
    artifacts = cast(list[dict[str, object]], metadata["artifacts"])
    artifacts[1]["sha256"] = _sha256(persona_path)
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    manifest_path = generation / "manifest.json"
    manifest = read_json(manifest_path)
    files = cast(list[dict[str, object]], manifest["files"])
    files[1]["sha256"] = _sha256(persona_path)
    files[2]["sha256"] = _sha256(metadata_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(BackupArtifactError):
        verify_backup(
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )


def test_backup_restore_round_trip_restores_both_sqlite_files_without_chroma(
    tmp_path: Path,
) -> None:
    from app.backup_restore import restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_pair_generation(tmp_path, repository_root)
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    create_persona_memory_database(
        destination, repository_root, with_approved_memory=False
    )
    destination.chroma_path.mkdir()
    chroma_sentinel = destination.chroma_path / "index-sentinel"
    chroma_sentinel.write_text("restore対象外", encoding="utf-8")

    restore_backup(
        runtime_paths=destination,
        repository_root=repository_root,
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )

    assert database_projection(destination.sqlite_path)[2] == CONVERSATION_SENTINEL
    assert persona_memory_projection(destination.persona_memory_sqlite_path) == (1, 1)
    assert chroma_sentinel.read_text(encoding="utf-8") == "restore対象外"


def test_persona_staging_validation_failure_preserves_both_live_databases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from app.backup_restore import BackupArtifactError, restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_pair_generation(tmp_path, repository_root)
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    destination_history = create_history_database(destination, wal=False)
    destination_history.execute(
        "UPDATE conversation_turns SET user_content = ? WHERE turn_id = ?",
        ("復元前の会話", TURN_ID),
    )
    destination_history.commit()
    destination_history.close()
    create_persona_memory_database(
        destination, repository_root, with_approved_memory=False
    )
    conversation_before = database_projection(destination.sqlite_path)
    persona_before = persona_memory_projection(destination.persona_memory_sqlite_path)
    copyfile = shutil.copyfile
    replace = Mock(wraps=os.replace)

    def corrupt_persona_staging(source: Path, target: Path) -> Path:
        copyfile(source, target)
        if source.name == "persona-memory.db":
            with target.open("ab") as artifact:
                artifact.write(b"corrupted-after-copy")
        return target

    monkeypatch.setattr(shutil, "copyfile", corrupt_persona_staging)
    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(BackupArtifactError):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    replace.assert_not_called()
    assert database_projection(destination.sqlite_path) == conversation_before
    assert (
        persona_memory_projection(destination.persona_memory_sqlite_path)
        == persona_before
    )


def test_initial_restore_stages_and_replaces_pair_when_one_database_already_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_pair_generation(tmp_path, repository_root)
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    shutil.copyfile(
        generation / "conversation-history.db",
        destination.sqlite_path,
    )
    create_persona_memory_database(
        destination, repository_root, with_approved_memory=False
    )
    replace = Mock(wraps=os.replace)
    monkeypatch.setattr(os, "replace", replace)

    restore_backup(
        runtime_paths=destination,
        repository_root=repository_root,
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )

    database_paths = {
        destination.sqlite_path,
        destination.persona_memory_sqlite_path,
    }
    assert [
        call.args[1]
        for call in replace.call_args_list
        if call.args[1] in database_paths
    ] == [destination.sqlite_path, destination.persona_memory_sqlite_path]


def test_initial_restore_validates_matching_database_before_any_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import BackupArtifactError, restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_pair_generation(tmp_path, repository_root)
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    shutil.copyfile(
        generation / "conversation-history.db",
        destination.sqlite_path,
    )
    create_persona_memory_database(
        destination, repository_root, with_approved_memory=False
    )
    conversation_before = database_projection(destination.sqlite_path)
    persona_before = persona_memory_projection(destination.persona_memory_sqlite_path)
    copyfile = shutil.copyfile
    replace = Mock(wraps=os.replace)

    def corrupt_matching_conversation_staging(source: Path, target: Path) -> Path:
        copyfile(source, target)
        if source.name == "conversation-history.db":
            with target.open("ab") as artifact:
                artifact.write(b"corrupted-after-copy")
        return target

    monkeypatch.setattr(shutil, "copyfile", corrupt_matching_conversation_staging)
    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(BackupArtifactError):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    replace.assert_not_called()
    assert database_projection(destination.sqlite_path) == conversation_before
    assert (
        persona_memory_projection(destination.persona_memory_sqlite_path)
        == persona_before
    )


@pytest.mark.parametrize("artifact_filename", ARTIFACT_FILENAMES)
def test_verify_rejects_tampering_of_each_artifact(
    tmp_path: Path,
    artifact_filename: str,
) -> None:
    from app.backup_restore import BackupArtifactError, verify_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _paths, generation = _create_pair_generation(tmp_path, repository_root)
    artifact = generation / artifact_filename
    artifact.write_bytes(artifact.read_bytes() + b"tampered")

    with pytest.raises(BackupArtifactError):
        verify_backup(
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )


@pytest.mark.parametrize("database_name", ARTIFACT_FILENAMES)
def test_backup_rejects_schema_version_mismatch_for_each_artifact(
    tmp_path: Path,
    database_name: str,
) -> None:
    from app.backup_restore import BackupSchemaError, create_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root, name="source")
    history = create_history_database(paths, wal=False)
    create_persona_memory_database(paths, repository_root, with_approved_memory=False)
    with sqlite3.connect(paths.data_root / database_name) as connection:
        connection.execute("PRAGMA user_version = 999")

    try:
        with pytest.raises(BackupSchemaError):
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
        history.close()


def test_second_replace_failure_requires_roll_forward_and_retry_completes_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreRecoveryRequiredError, restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_pair_generation(tmp_path, repository_root)
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    create_persona_memory_database(
        destination, repository_root, with_approved_memory=False
    )
    real_replace = _inject_second_replace_failure(monkeypatch)
    with pytest.raises(RestoreRecoveryRequiredError):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )
    assert destination.restore_intent_path.is_file()

    retry_replace = Mock(wraps=real_replace)
    monkeypatch.setattr(os, "replace", retry_replace)

    restore_backup(
        runtime_paths=destination,
        repository_root=repository_root,
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )

    assert [call.args[1] for call in retry_replace.call_args_list] == [
        destination.persona_memory_sqlite_path
    ]
    assert not destination.restore_intent_path.exists()
    assert database_projection(destination.sqlite_path)[2] == CONVERSATION_SENTINEL
    assert persona_memory_projection(destination.persona_memory_sqlite_path) == (1, 1)


def test_interrupted_restore_intent_records_fixed_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreRecoveryRequiredError, restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_pair_generation(tmp_path, repository_root)
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    create_persona_memory_database(
        destination, repository_root, with_approved_memory=False
    )
    _inject_second_replace_failure(monkeypatch)

    with pytest.raises(RestoreRecoveryRequiredError):
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

    intent = read_json(destination.restore_intent_path)
    assert intent["artifacts"] == [
        {
            "filename": filename,
            "sha256": _sha256(generation / filename),
        }
        for filename in ARTIFACT_FILENAMES
    ]


def test_restore_intent_blocks_persona_sqlite_before_connection_open(
    tmp_path: Path,
) -> None:
    from unittest.mock import Mock

    from app.backup_restore import RestoreRecoveryRequiredError
    from app.memory.persistence.sqlite import PersonaMemorySqlite

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    paths.restore_intent_path.write_text("{}\n", encoding="utf-8")
    connection_factory = Mock(side_effect=AssertionError("SQLite must stay closed"))
    database = PersonaMemorySqlite(paths.persona_memory_sqlite_path, connection_factory)

    with pytest.raises(RestoreRecoveryRequiredError):
        with database.connection():
            pytest.fail("persona memory must remain unavailable during recovery")
    connection_factory.assert_not_called()


@pytest.mark.parametrize("format_version", (2, 4))
@pytest.mark.parametrize("operation", ("verify", "restore"))
def test_non_current_format_generation_is_explicitly_rejected(
    tmp_path: Path,
    operation: str,
    format_version: int,
) -> None:
    from app.backup_restore import BackupArtifactError, restore_backup, verify_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_pair_generation(tmp_path, repository_root)
    metadata_path = generation / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["formatVersion"] = format_version
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(BackupArtifactError, match="unsupported"):
        if operation == "verify":
            verify_backup(
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )
        else:
            destination = initialized_runtime(
                tmp_path, repository_root, name="destination"
            )
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )


def test_legacy_single_database_intent_marker_is_not_a_recovery_marker(
    tmp_path: Path,
) -> None:
    from app.backup_restore import create_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    history = create_history_database(paths, wal=False)
    legacy_marker = paths.data_root / ".conversation-history.restore-intent.json"
    legacy_marker.write_text("{}\n", encoding="utf-8")
    try:
        generation = create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=tmp_path / "backups",
            retention_count=3,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )
    finally:
        history.close()

    assert generation.is_dir()
    assert legacy_marker.is_file()


@pytest.mark.parametrize("database_name", ARTIFACT_FILENAMES)
def test_restore_rejects_when_either_database_has_a_runtime_lease(
    tmp_path: Path,
    database_name: str,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.conversation_history.sqlite_lease import acquire_runtime_lease

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, generation = _create_pair_generation(tmp_path, repository_root)
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    database = destination.data_root / database_name

    with acquire_runtime_lease(database):
        with pytest.raises(RestoreSafetyError):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )


def test_conversation_and_persona_databases_have_independent_maintenance_leases(
    tmp_path: Path,
) -> None:
    from app.conversation_history.sqlite_lease import acquire_maintenance_lease

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)

    with acquire_maintenance_lease(paths.sqlite_path) as conversation_lease:
        with acquire_maintenance_lease(
            paths.persona_memory_sqlite_path
        ) as persona_lease:
            conversation_lease.require_maintenance_for(paths.sqlite_path)
            persona_lease.require_maintenance_for(paths.persona_memory_sqlite_path)

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.backup_restore.models import CONVERSATION_ARTIFACT_FILENAME
from app.conversation_history.schema import CURRENT_TABLES, SCHEMA_VERSION
from app.memory.persistence.schema import initialize_persona_memory_schema
from tests.backup_restore_test_support import (
    CONVERSATION_SENTINEL,
    FIXED_BACKUP_TIME,
    FIXED_COMMIT,
    SECRET_SENTINEL,
    TEST_AUTHENTICATION_KEY,
    create_history_database,
    database_projection,
    initialized_runtime,
    read_json,
)


def _create_backup(tmp_path: Path, repository_root: Path):
    from app.backup_restore import create_backup

    paths = initialized_runtime(tmp_path, repository_root)
    live_connection = create_history_database(paths, wal=True)
    backup_root = tmp_path / "backups"
    generation = create_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_root=backup_root,
        retention_count=3,
        authentication_key=TEST_AUTHENTICATION_KEY,
        git_commit=FIXED_COMMIT,
        created_at=FIXED_BACKUP_TIME,
    )
    return paths, live_connection, backup_root, generation


def _data_root_snapshot(data_root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(data_root)): path.read_bytes() if path.is_file() else None
        for path in data_root.rglob("*")
    }


def _record_authentication_comparisons(
    monkeypatch: pytest.MonkeyPatch,
) -> list[None]:
    from app.backup_restore import contracts

    compare_digest = contracts.hmac.compare_digest
    calls: list[None] = []

    def record_authentication_comparison(
        actual: str | bytes, expected: str | bytes
    ) -> bool:
        calls.append(None)
        return compare_digest(actual, expected)

    monkeypatch.setattr(
        contracts.hmac, "compare_digest", record_authentication_comparison
    )
    return calls


def test_should_authenticate_staging_and_published_generation_once_each(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import create_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    read_verified_generation = service.read_verified_generation
    authenticated_directories: list[Path] = []

    def record_authenticated_directory(directory: Path, authentication_key):
        authenticated_directories.append(directory)
        return read_verified_generation(directory, authentication_key)

    monkeypatch.setattr(
        service, "read_verified_generation", record_authenticated_directory
    )
    try:
        generation = create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=tmp_path / "backups",
            retention_count=1,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )

        staging_directories = [
            directory
            for directory in authenticated_directories
            if directory.name.startswith(".backup-staging-")
        ]
        assert len(authenticated_directories) == 2
        assert len(staging_directories) == 1
        assert authenticated_directories.count(staging_directories[0]) == 1
        assert authenticated_directories.count(generation) == 1
    finally:
        connection.close()


def test_should_verify_new_generation_once_per_safety_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import create_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    verify_sqlite_database = service.verify_sqlite_database
    verified_artifacts: list[Path] = []

    def record_sqlite_verification(artifact: Path, filename: str):
        verified_artifacts.append(artifact)
        return verify_sqlite_database(artifact, filename)

    monkeypatch.setattr(
        service, "verify_sqlite_database", record_sqlite_verification
    )
    try:
        create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=tmp_path / "backups",
            retention_count=1,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )

        assert len(verified_artifacts) == 4
        assert all(
            artifact.parent.name.startswith((".backup-staging-", "backup-"))
            for artifact in verified_artifacts
        )
    finally:
        connection.close()


def test_bkp_wal_01_includes_committed_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()

    paths, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    try:
        assert paths.sqlite_path.with_name(paths.sqlite_path.name + "-wal").stat().st_size > 0
        assert database_projection(generation / "conversation-history.db") == (
            SCHEMA_VERSION,
            1,
            CONVERSATION_SENTINEL,
        )
    finally:
        live_connection.close()


def test_bkp_meta_01_writes_strict_safe_metadata_and_manifest(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    try:
        metadata = read_json(generation / "metadata.json")
        manifest = read_json(generation / "manifest.json")

        assert set(metadata) == {
            "formatVersion",
            "environmentId",
            "gitCommit",
            "createdAt",
            "generationSequence",
            "artifacts",
        }
        assert metadata["formatVersion"] == 3
        assert metadata["environmentId"] == paths.environment_id
        assert metadata["gitCommit"] == FIXED_COMMIT
        assert metadata["createdAt"] == "2026-08-08T01:02:03Z"
        assert isinstance(metadata["generationSequence"], int)
        assert metadata["generationSequence"] > 0
        artifacts = metadata["artifacts"]
        assert [artifact["filename"] for artifact in artifacts] == [
            "conversation-history.db",
            "persona-memory.db",
        ]
        assert artifacts[0]["schemaVersion"] == SCHEMA_VERSION
        assert artifacts[0]["integrityCheck"] == "ok"
        assert artifacts[0]["recordCount"] == 1
        assert all(len(artifact["sha256"]) == 64 for artifact in artifacts)
        assert manifest["formatVersion"] == 3
        assert manifest["complete"] is True
        assert [entry["path"] for entry in manifest["files"]] == [
            "conversation-history.db",
            "persona-memory.db",
            "metadata.json",
        ]
        assert all(len(entry["sha256"]) == 64 for entry in manifest["files"])
        assert len(manifest["authenticationHmacSha256"]) == 64
    finally:
        live_connection.close()


def test_bkp_priv_01_does_not_leak_content_or_secrets(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    monkeypatch.setenv("BACKUP_SECRET_SENTINEL", SECRET_SENTINEL)

    with caplog.at_level(logging.INFO):
        _paths, live_connection, _backup_root, generation = _create_backup(
            tmp_path, repository_root
        )
    try:
        captured = capsys.readouterr()
        exposed_text = "\n".join(
            (
                (generation / "metadata.json").read_text(encoding="utf-8"),
                (generation / "manifest.json").read_text(encoding="utf-8"),
                caplog.text,
                captured.out,
                captured.err,
            )
        )
        assert CONVERSATION_SENTINEL not in exposed_text
        assert SECRET_SENTINEL not in exposed_text
        assert ("ab" * 32) not in exposed_text
    finally:
        live_connection.close()


def test_bkp_verify_01_reports_integrity_schema_tables_and_counts(tmp_path: Path) -> None:
    from app.backup_restore import verify_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _paths, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    try:
        result = verify_backup(
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )
        conversation = result.artifact(CONVERSATION_ARTIFACT_FILENAME)

        assert conversation.integrity_check == "ok"
        assert conversation.schema_version == SCHEMA_VERSION
        assert conversation.required_tables == CURRENT_TABLES
        assert conversation.record_count == 1
    finally:
        live_connection.close()


def test_should_authenticate_generation_once_per_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import verify_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _paths, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    authentication_comparisons = _record_authentication_comparisons(monkeypatch)
    try:
        verify_backup(
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

        assert len(authentication_comparisons) == 1
    finally:
        live_connection.close()


@pytest.mark.parametrize("invalid_sequence", (0, -1, True, "1"))
def test_should_reject_invalid_authenticated_generation_sequence(
    tmp_path: Path,
    invalid_sequence: int | bool | str,
) -> None:
    from app.backup_restore import BackupArtifactError, verify_backup
    from app.backup_restore.contracts import write_contract_files

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _paths, connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    metadata = read_json(generation / "metadata.json")
    metadata["generationSequence"] = invalid_sequence
    write_contract_files(generation, metadata, TEST_AUTHENTICATION_KEY)
    try:
        with pytest.raises(BackupArtifactError):
            verify_backup(
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )
    finally:
        connection.close()


def test_should_reject_backup_verified_with_different_authentication_key(
    tmp_path: Path,
) -> None:
    from app.backup_restore import (
        BackupArtifactError,
        BackupAuthenticationKey,
        verify_backup,
    )

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _paths, connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    different_key = BackupAuthenticationKey(bytes.fromhex("cd" * 32))
    try:
        with pytest.raises(BackupArtifactError, match="authentication"):
            verify_backup(
                backup_directory=generation,
                authentication_key=different_key,
            )
    finally:
        connection.close()


def test_should_create_distinct_verified_generations_for_same_time_and_commit(
    tmp_path: Path,
) -> None:
    from app.backup_restore import create_backup, verify_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    backup_root = tmp_path / "backups"
    try:
        generations = tuple(
            create_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_root=backup_root,
                retention_count=2,
                authentication_key=TEST_AUTHENTICATION_KEY,
                git_commit=FIXED_COMMIT,
                created_at=FIXED_BACKUP_TIME,
            )
            for _ in range(2)
        )

        assert generations[0] != generations[1]
        assert all(generation.is_dir() for generation in generations)
        assert all(
            verify_backup(
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            ).artifact(CONVERSATION_ARTIFACT_FILENAME).record_count
            == 1
            for generation in generations
        )
    finally:
        connection.close()


def test_bkp_verify_01_does_not_publish_generation_for_invalid_source_schema(
    tmp_path: Path,
) -> None:
    from app.backup_restore import BackupSchemaError, create_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    import sqlite3

    with sqlite3.connect(paths.sqlite_path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    backup_root = tmp_path / "backups"

    with pytest.raises(BackupSchemaError, match="schema|table"):
        create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=3,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )

    backup_entries = () if not backup_root.exists() else tuple(backup_root.iterdir())
    assert not any(entry.name.startswith("backup-") for entry in backup_entries)
    assert not any(entry.name.startswith(".backup-staging-") for entry in backup_entries)


def test_bkp_path_01_rejects_backup_root_inside_runtime_data_root(tmp_path: Path) -> None:
    from app.backup_restore import create_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    try:
        with pytest.raises(ValueError, match="backup.*data root|overlap"):
            create_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_root=paths.data_root / "backups",
                retention_count=3,
                authentication_key=TEST_AUTHENTICATION_KEY,
                git_commit=FIXED_COMMIT,
                created_at=FIXED_BACKUP_TIME,
            )
    finally:
        connection.close()


def test_bkp_chroma_01_only_packages_sqlite_and_contract_json(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    paths.chroma_path.mkdir()
    (paths.chroma_path / "derived-secret.bin").write_text(
        CONVERSATION_SENTINEL, encoding="utf-8"
    )
    live_connection = create_history_database(paths, wal=False)
    from app.backup_restore import create_backup

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

        assert {entry.name for entry in generation.iterdir()} == {
            "conversation-history.db",
            "persona-memory.db",
            "metadata.json",
            "manifest.json",
        }
        assert database_projection(generation / "conversation-history.db")[2] == (
            CONVERSATION_SENTINEL
        )
    finally:
        live_connection.close()


@pytest.mark.parametrize(
    "mutation",
    ("artifact", "metadata", "missing-manifest", "incomplete"),
)
def test_rst_preflight_01_rejects_tampered_or_incomplete_generation_before_write(
    tmp_path: Path, mutation: str
) -> None:
    from app.backup_restore import BackupArtifactError, restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _paths, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    destination_connection = create_history_database(destination, wal=False)
    destination_connection.close()
    (destination.data_root / "keep.txt").write_text("unchanged", encoding="utf-8")
    before = _data_root_snapshot(destination.data_root)
    try:
        if mutation == "artifact":
            with (generation / "conversation-history.db").open("ab") as artifact:
                artifact.write(b"tampered")
        elif mutation == "metadata":
            metadata = read_json(generation / "metadata.json")
            metadata["conversationCount"] = 999
            (generation / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        elif mutation == "missing-manifest":
            (generation / "manifest.json").unlink()
        else:
            manifest = read_json(generation / "manifest.json")
            manifest["complete"] = False
            (generation / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(
            BackupArtifactError,
            match="artifact|metadata|manifest|complete|checksum",
        ):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )

        assert _data_root_snapshot(destination.data_root) == before
    finally:
        live_connection.close()


def test_should_authenticate_generation_once_during_restore_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    authentication_comparisons = _record_authentication_comparisons(monkeypatch)
    try:
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

        assert len(authentication_comparisons) == 1
    finally:
        live_connection.close()


def test_rst_id_01_rejects_environment_mismatch_without_changing_destination(
    tmp_path: Path,
) -> None:
    from app.backup_restore import BackupIdentityError, restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(
        tmp_path, repository_root, environment_id="dev", name="destination"
    )
    sentinel = destination.data_root / "keep.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    try:
        with pytest.raises(BackupIdentityError, match="identity|environment"):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )

        assert sentinel.read_text(encoding="utf-8") == "unchanged"
        assert not destination.sqlite_path.exists()
    finally:
        live_connection.close()


def test_rst_preflight_01_rejects_coherently_tampered_generation_before_write(
    tmp_path: Path,
) -> None:
    from app.backup_restore import BackupArtifactError, restore_backup
    import sqlite3

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    artifact = generation / "conversation-history.db"
    metadata_path = generation / "metadata.json"
    manifest_path = generation / "manifest.json"
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    destination_connection = create_history_database(destination, wal=False)
    destination_connection.close()
    (destination.data_root / "keep.txt").write_text("unchanged", encoding="utf-8")
    before = _data_root_snapshot(destination.data_root)
    try:
        with sqlite3.connect(artifact) as connection:
            connection.execute("PRAGMA user_version = 2")
        metadata = read_json(metadata_path)
        metadata["artifacts"][0]["sha256"] = hashlib.sha256(
            artifact.read_bytes()
        ).hexdigest()
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        manifest = read_json(manifest_path)
        for entry in manifest["files"]:
            file_path = generation / entry["path"]
            entry["sha256"] = hashlib.sha256(file_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(BackupArtifactError, match="authentication"):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )

        assert _data_root_snapshot(destination.data_root) == before
    finally:
        live_connection.close()


def test_rst_preflight_01_rejects_authenticated_schema_metadata_mismatch(
    tmp_path: Path,
) -> None:
    from app.backup_restore import BackupSchemaError, restore_backup
    from app.backup_restore.contracts import write_contract_files

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    metadata = read_json(generation / "metadata.json")
    metadata["artifacts"][0]["schemaVersion"] = 2
    write_contract_files(generation, metadata, TEST_AUTHENTICATION_KEY)
    try:
        with pytest.raises(BackupSchemaError, match="schema"):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )

        assert not destination.sqlite_path.exists()
    finally:
        live_connection.close()


def test_rst_safe_01_preflight_failure_preserves_existing_database_bytes(
    tmp_path: Path,
) -> None:
    from app.backup_restore import BackupArtifactError, restore_backup
    from app.conversation_history.sqlite_lease import (
        PERSONA_MEMORY_SQLITE_LEASE_FILENAME_SUFFIX,
        SQLITE_LEASE_FILENAME_SUFFIX,
    )

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    destination_connection = create_history_database(destination, wal=False)
    destination_connection.close()
    before = destination.sqlite_path.read_bytes()
    with (generation / "conversation-history.db").open("ab") as artifact:
        artifact.write(b"corrupt")
    try:
        with pytest.raises(BackupArtifactError):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )

        assert destination.sqlite_path.read_bytes() == before
        assert {entry.name for entry in destination.data_root.iterdir()} == {
            ".environment-identity.json",
            ".environment-identity.lock",
            SQLITE_LEASE_FILENAME_SUFFIX,
            PERSONA_MEMORY_SQLITE_LEASE_FILENAME_SUFFIX,
            "conversation-history.db",
        }
    finally:
        live_connection.close()


def test_should_reject_artifact_changed_during_copy_before_database_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import BackupArtifactError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    destination_connection = create_history_database(destination, wal=False)
    destination_connection.close()
    before = _data_root_snapshot(destination.data_root)
    copyfile = service.shutil.copyfile

    def copy_then_mutate(source: Path, target: Path) -> str:
        result = copyfile(source, target)
        with target.open("ab") as copied_artifact:
            copied_artifact.write(b"changed-during-copy")
        return result

    monkeypatch.setattr(service.shutil, "copyfile", copy_then_mutate)
    try:
        with pytest.raises(BackupArtifactError, match="checksum"):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )

        assert _data_root_snapshot(destination.data_root) == before
    finally:
        live_connection.close()


def test_rst_safe_01_replace_failure_preserves_entire_existing_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    destination_connection = create_history_database(destination, wal=False)
    destination_connection.close()
    (destination.data_root / "keep.txt").write_text("unchanged", encoding="utf-8")
    before = _data_root_snapshot(destination.data_root)
    monkeypatch.setattr(
        service.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected database replacement failure")
        ),
    )
    try:
        with pytest.raises(
            RestoreSafetyError,
            match="replacement|restore|safety",
        ):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )

        assert _data_root_snapshot(destination.data_root) == before
    finally:
        live_connection.close()


def test_should_reject_destination_with_active_wal_before_replacement(
    tmp_path: Path,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, source_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    destination_connection = create_history_database(destination, wal=True)
    before = _data_root_snapshot(destination.data_root)
    try:
        with pytest.raises(RestoreSafetyError, match="sidecar"):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )

        assert _data_root_snapshot(destination.data_root) == before
    finally:
        destination_connection.close()
        source_connection.close()


@pytest.mark.parametrize(
    ("database_name", "suffix"),
    (
        ("conversation-history.db", "-journal"),
        ("conversation-history.db", "-shm"),
        ("conversation-history.db", "-wal"),
        ("persona-memory.db", "-journal"),
        ("persona-memory.db", "-shm"),
        ("persona-memory.db", "-wal"),
    ),
)
def test_should_reject_malformed_destination_sidecar_before_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_name: str,
    suffix: str,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, source_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    destination_connection = create_history_database(destination, wal=False)
    destination_connection.close()
    initialize_persona_memory_schema(destination, repository_root)
    database_path = destination.data_root / database_name
    database_path.with_name(database_path.name + suffix).write_bytes(b"malformed-sidecar")
    before = _data_root_snapshot(destination.data_root)
    replace = Mock(wraps=os.replace)
    monkeypatch.setattr(service.os, "replace", replace)
    try:
        with pytest.raises(RestoreSafetyError, match="sidecar"):
            restore_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )

        replace.assert_not_called()
        assert _data_root_snapshot(destination.data_root) == before
    finally:
        source_connection.close()


def test_rst_post_01_atomically_replaces_existing_database(tmp_path: Path) -> None:
    from app.backup_restore import restore_backup, verify_restored_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    destination_connection = create_history_database(destination, wal=False)
    destination_connection.close()
    try:
        restored = restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )

        assert restored == verify_restored_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )
        assert destination.sqlite_path.read_bytes() == (
            generation / "conversation-history.db"
        ).read_bytes()
    finally:
        live_connection.close()


def test_rst_post_01_restores_to_initialized_empty_root_and_revalidates(tmp_path: Path) -> None:
    from app.backup_restore import restore_backup, verify_restored_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    try:
        restored = restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )
        verified = verify_restored_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )
        restored_conversation = restored.artifact(CONVERSATION_ARTIFACT_FILENAME)
        verified_conversation = verified.artifact(CONVERSATION_ARTIFACT_FILENAME)

        assert restored_conversation.schema_version == SCHEMA_VERSION
        assert restored_conversation.record_count == 1
        assert verified_conversation.schema_version == SCHEMA_VERSION
        assert verified_conversation.required_tables == CURRENT_TABLES
        assert verified_conversation.record_count == 1
        assert database_projection(destination.sqlite_path)[2] == CONVERSATION_SENTINEL
    finally:
        live_connection.close()


def test_should_reject_restored_database_with_different_conversation_count(
    tmp_path: Path,
) -> None:
    from app.backup_restore import (
        BackupArtifactError,
        restore_backup,
        verify_restored_backup,
    )

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, live_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    try:
        restore_backup(
            runtime_paths=destination,
            repository_root=repository_root,
            backup_directory=generation,
            authentication_key=TEST_AUTHENTICATION_KEY,
        )
        with sqlite3.connect(destination.sqlite_path) as connection:
            connection.execute(
                "INSERT INTO conversations (character_id, conversation_id, created_at) "
                "VALUES (?, ?, ?)",
                (
                    "miori",
                    "00000000-0000-4000-8000-000000000001",
                    "2026-08-08T02:00:00.000000Z",
                ),
            )

        with pytest.raises(BackupArtifactError):
            verify_restored_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )
    finally:
        live_connection.close()


def test_retention_01_removes_only_old_complete_generations(tmp_path: Path) -> None:
    from app.backup_restore import create_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    backup_root = tmp_path / "backups"
    generations = [
        create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=4,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME + timedelta(days=index),
        )
        for index in range(3)
    ]
    unknown_file = backup_root / "operator-notes.txt"
    unknown_file.write_text("keep", encoding="utf-8")
    unknown_directory = backup_root / "manual-copy"
    unknown_directory.mkdir()
    incomplete = backup_root / "backup-incomplete"
    incomplete.mkdir()
    corrupt = backup_root / "backup-corrupt"
    shutil.copytree(generations[-1], corrupt)
    with (corrupt / "persona-memory.db").open("ab") as artifact:
        artifact.write(b"corrupt")
    symlink = backup_root / "backup-20260801T000000Z-symlink"
    symlink.symlink_to(generations[-1], target_is_directory=True)

    generations.append(
        create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=2,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME + timedelta(days=3),
        )
    )

    assert not generations[0].exists()
    assert not generations[1].exists()
    assert generations[2].is_dir()
    assert generations[3].is_dir()
    assert unknown_file.is_file()
    assert unknown_directory.is_dir()
    assert incomplete.is_dir()
    assert corrupt.is_dir()
    assert symlink.is_symlink()
    connection.close()


def test_should_fail_creation_without_pruning_when_published_generation_is_corrupted(
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
    previous = create_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_root=backup_root,
        retention_count=2,
        authentication_key=TEST_AUTHENTICATION_KEY,
        git_commit=FIXED_COMMIT,
        created_at=FIXED_BACKUP_TIME,
    )
    fsync_directory = service._fsync_directory
    published: Path | None = None

    def corrupt_after_publication(path: Path) -> None:
        nonlocal published
        fsync_directory(path)
        if path != backup_root or published is not None:
            return
        published = next(
            generation
            for generation in backup_root.glob("backup-*")
            if generation != previous
        )
        with (published / "conversation-history.db").open("ab") as artifact:
            artifact.write(b"corrupt-after-publication")

    monkeypatch.setattr(service, "_fsync_directory", corrupt_after_publication)
    try:
        with pytest.raises(BackupArtifactError):
            create_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_root=backup_root,
                retention_count=1,
                authentication_key=TEST_AUTHENTICATION_KEY,
                git_commit=FIXED_COMMIT,
                created_at=FIXED_BACKUP_TIME + timedelta(days=1),
            )

        assert published is not None
        assert previous.is_dir()
        assert published.is_dir()
    finally:
        connection.close()


def test_should_fail_creation_without_pruning_when_published_generation_is_replaced(
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
    previous = create_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_root=backup_root,
        retention_count=2,
        authentication_key=TEST_AUTHENTICATION_KEY,
        git_commit=FIXED_COMMIT,
        created_at=FIXED_BACKUP_TIME,
    )
    replacement = create_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_root=tmp_path / "replacement-backups",
        retention_count=1,
        authentication_key=TEST_AUTHENTICATION_KEY,
        git_commit=FIXED_COMMIT,
        created_at=FIXED_BACKUP_TIME + timedelta(days=2),
    )
    replacement_metadata = (replacement / "metadata.json").read_bytes()
    fsync_directory = service._fsync_directory
    published: Path | None = None

    def replace_after_publication(path: Path) -> None:
        nonlocal published
        fsync_directory(path)
        if path != backup_root or published is not None:
            return
        published = next(
            generation
            for generation in backup_root.glob("backup-*")
            if generation != previous
        )
        shutil.rmtree(published)
        shutil.copytree(replacement, published)

    monkeypatch.setattr(service, "_fsync_directory", replace_after_publication)
    try:
        with pytest.raises(BackupArtifactError):
            create_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_root=backup_root,
                retention_count=1,
                authentication_key=TEST_AUTHENTICATION_KEY,
                git_commit=FIXED_COMMIT,
                created_at=FIXED_BACKUP_TIME + timedelta(days=1),
            )

        assert published is not None
        assert previous.is_dir()
        assert published.is_dir()
        assert (published / "metadata.json").read_bytes() == replacement_metadata
    finally:
        connection.close()


def test_should_not_delete_candidate_corrupted_after_initial_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import create_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    backup_root = tmp_path / "backups"
    generations = tuple(
        create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=3,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME + timedelta(days=index),
        )
        for index in range(2)
    )
    candidate = generations[0]
    preflight = service._preflight
    changed = False

    def corrupt_after_initial_verification(path: Path, authentication_key):
        nonlocal changed
        verified = preflight(path, authentication_key)
        if path == candidate and not changed:
            changed = True
            with (candidate / "conversation-history.db").open("ab") as artifact:
                artifact.write(b"corrupt-before-delete")
        return verified

    monkeypatch.setattr(service, "_preflight", corrupt_after_initial_verification)
    try:
        create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=1,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME + timedelta(days=2),
        )

        assert changed
        assert candidate.is_dir()
    finally:
        connection.close()


def test_should_not_delete_candidate_replaced_after_initial_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import create_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    backup_root = tmp_path / "backups"
    generations = tuple(
        create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=3,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME + timedelta(days=index),
        )
        for index in range(2)
    )
    candidate = generations[0]
    replacement = create_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_root=tmp_path / "replacement-backups",
        retention_count=1,
        authentication_key=TEST_AUTHENTICATION_KEY,
        git_commit=FIXED_COMMIT,
        created_at=FIXED_BACKUP_TIME + timedelta(days=3),
    )
    replacement_metadata = (replacement / "metadata.json").read_bytes()
    preflight = service._preflight
    changed = False

    def replace_after_initial_verification(path: Path, authentication_key):
        nonlocal changed
        verified = preflight(path, authentication_key)
        if path == candidate and not changed:
            changed = True
            shutil.rmtree(candidate)
            shutil.copytree(replacement, candidate)
        return verified

    monkeypatch.setattr(service, "_preflight", replace_after_initial_verification)
    try:
        create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=1,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME + timedelta(days=2),
        )

        assert changed
        assert candidate.is_dir()
        assert (candidate / "metadata.json").read_bytes() == replacement_metadata
    finally:
        connection.close()


def test_should_scan_existing_generations_once_per_backup_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import create_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    backup_root = tmp_path / "backups"
    create_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_root=backup_root,
        retention_count=2,
        authentication_key=TEST_AUTHENTICATION_KEY,
        git_commit=FIXED_COMMIT,
        created_at=FIXED_BACKUP_TIME,
    )
    iterdir = Path.iterdir
    existing_generation_scans = 0

    def count_existing_generation_scan(path: Path) -> Iterator[Path]:
        nonlocal existing_generation_scans
        if path == backup_root:
            existing_generation_scans += 1
        return iterdir(path)

    monkeypatch.setattr(Path, "iterdir", count_existing_generation_scan)
    try:
        create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=1,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME + timedelta(days=1),
        )

        assert existing_generation_scans == 1
    finally:
        connection.close()


def test_should_not_prune_existing_generations_when_backup_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import create_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    backup_root = tmp_path / "backups"
    generations = tuple(
        create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=2,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME + timedelta(days=index),
        )
        for index in range(2)
    )

    def fail_snapshot(_source: Path, _destination: Path) -> None:
        raise OSError("snapshot failed")

    monkeypatch.setattr(service, "create_sqlite_snapshot", fail_snapshot)
    try:
        with pytest.raises(OSError, match="snapshot failed"):
            create_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_root=backup_root,
                retention_count=1,
                authentication_key=TEST_AUTHENTICATION_KEY,
                git_commit=FIXED_COMMIT,
                created_at=FIXED_BACKUP_TIME + timedelta(days=2),
            )

        assert all(generation.is_dir() for generation in generations)
        assert not tuple(backup_root.glob(".backup-staging-*"))
    finally:
        connection.close()


def test_should_retain_later_generation_when_timestamp_and_uuid_sort_are_reversed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import create_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    backup_root = tmp_path / "backups"
    unique_ids = iter(
        (
            UUID("10000000-0000-4000-8000-000000000000"),
            UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
            UUID("20000000-0000-4000-8000-000000000000"),
            UUID("00000000-0000-4000-8000-000000000000"),
        )
    )
    generation_times = iter((200, 100))
    monkeypatch.setattr(service, "uuid4", lambda: next(unique_ids))
    monkeypatch.setattr(service, "time_ns", lambda: next(generation_times), raising=False)
    try:
        earlier = create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=2,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )
        earlier_sequence = read_json(earlier / "metadata.json")["generationSequence"]
        later = create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=1,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )
        later_sequence = read_json(later / "metadata.json")["generationSequence"]

        assert earlier_sequence == 200
        assert later_sequence == 201
        assert not earlier.exists()
        assert later.is_dir()
    finally:
        connection.close()


def test_should_serialize_concurrent_generation_creation_and_retain_later_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backup_restore import create_backup
    from app.backup_restore import service

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    backup_root = tmp_path / "backups"
    start = Barrier(2)
    generated_sequences: list[int] = []
    original_write_contract_files = service.write_contract_files

    def fixed_sequence_time() -> int:
        time.sleep(0.05)
        return 100

    def record_contract(*args, **kwargs) -> None:
        metadata = args[1]
        sequence = metadata["generationSequence"]
        assert isinstance(sequence, int)
        generated_sequences.append(sequence)
        original_write_contract_files(*args, **kwargs)

    def create_generation() -> Path:
        start.wait()
        return create_backup(
            runtime_paths=paths,
            repository_root=repository_root,
            backup_root=backup_root,
            retention_count=1,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )

    monkeypatch.setattr(service, "time_ns", fixed_sequence_time)
    monkeypatch.setattr(service, "write_contract_files", record_contract)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            generations = tuple(executor.map(lambda _index: create_generation(), range(2)))

        remaining = tuple(generation for generation in generations if generation.exists())
        remaining_sequence = read_json(remaining[0] / "metadata.json")[
            "generationSequence"
        ]

        assert sorted(generated_sequences) == [100, 101]
        assert len(remaining) == 1
        assert remaining_sequence == 101
    finally:
        connection.close()


@pytest.mark.parametrize("retention_count", (0, -1))
def test_retention_01_rejects_non_positive_limit(
    tmp_path: Path, retention_count: int
) -> None:
    from app.backup_restore import create_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root)
    connection = create_history_database(paths, wal=False)
    try:
        with pytest.raises(ValueError, match="positive"):
            create_backup(
                runtime_paths=paths,
                repository_root=repository_root,
                backup_root=tmp_path / "backups",
                retention_count=retention_count,
                authentication_key=TEST_AUTHENTICATION_KEY,
                git_commit=FIXED_COMMIT,
                created_at=FIXED_BACKUP_TIME,
            )
    finally:
        connection.close()

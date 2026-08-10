from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest

from app.conversation_history.schema import CURRENT_TABLES, SCHEMA_VERSION
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
            "schemaVersion",
            "createdAt",
            "generationSequence",
            "sqliteValidation",
            "conversationCount",
            "artifactSha256",
        }
        assert metadata["formatVersion"] == 2
        assert metadata["environmentId"] == paths.environment_id
        assert metadata["gitCommit"] == FIXED_COMMIT
        assert metadata["schemaVersion"] == SCHEMA_VERSION
        assert metadata["createdAt"] == "2026-08-08T01:02:03Z"
        assert isinstance(metadata["generationSequence"], int)
        assert metadata["generationSequence"] > 0
        assert metadata["sqliteValidation"] == "ok"
        assert metadata["conversationCount"] == 1
        assert len(metadata["artifactSha256"]) == 64
        assert manifest["formatVersion"] == 2
        assert manifest["complete"] is True
        assert [entry["path"] for entry in manifest["files"]] == [
            "conversation-history.db",
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

        assert result.integrity_check == "ok"
        assert result.schema_version == SCHEMA_VERSION
        assert result.required_tables == CURRENT_TABLES
        assert result.conversation_count == 1
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
            ).conversation_count
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
        metadata["artifactSha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
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
    metadata["schemaVersion"] = 2
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


@pytest.mark.parametrize("suffix", ("-journal", "-shm", "-wal"))
def test_should_reject_malformed_destination_sidecar_before_replacement(
    tmp_path: Path,
    suffix: str,
) -> None:
    from app.backup_restore import RestoreSafetyError, restore_backup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _source, source_connection, _backup_root, generation = _create_backup(
        tmp_path, repository_root
    )
    destination = initialized_runtime(tmp_path, repository_root, name="destination")
    destination_connection = create_history_database(destination, wal=False)
    destination_connection.close()
    destination.sqlite_path.with_name(destination.sqlite_path.name + suffix).write_bytes(
        b"malformed-sidecar"
    )
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

        assert restored.schema_version == SCHEMA_VERSION
        assert restored.conversation_count == 1
        assert verified.schema_version == SCHEMA_VERSION
        assert verified.required_tables == CURRENT_TABLES
        assert verified.conversation_count == 1
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
    assert symlink.is_symlink()
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

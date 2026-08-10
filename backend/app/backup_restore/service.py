from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from time import time_ns
from typing import Iterator
from uuid import uuid4

from app.backup_restore.contracts import (
    JsonValue,
    read_verified_generation,
    sha256_file,
    verified_generation,
    write_contract_files,
)
from app.backup_restore.models import (
    ARTIFACT_FILENAME,
    FORMAT_VERSION,
    GENERATION_PREFIX,
    MANIFEST_FILENAME,
    METADATA_FILENAME,
    BackupAuthenticationKey,
    BackupArtifactError,
    BackupError,
    BackupIdentityError,
    BackupVerification,
    RestoreSafetyError,
    VerifiedGeneration,
)
from app.backup_restore.sqlite_snapshot import (
    create_sqlite_snapshot,
    verify_sqlite_database,
)
from app.runtime_data_root import validate_existing_runtime_data_root
from app.runtime_paths import RuntimePaths

SQLITE_SIDECAR_SUFFIXES = ("-journal", "-shm", "-wal")
GENERATION_LOCK_FILENAME = ".backup-generation.lock"


def create_backup(
    *,
    runtime_paths: RuntimePaths,
    repository_root: Path,
    backup_root: Path,
    retention_count: int,
    authentication_key: BackupAuthenticationKey,
    git_commit: str | None = None,
    created_at: datetime | None = None,
) -> Path:
    validate_existing_runtime_data_root(runtime_paths, repository_root)
    _validate_backup_root(backup_root, runtime_paths.data_root)
    if retention_count <= 0:
        raise ValueError("retention count must be positive")
    commit = _git_commit(repository_root) if git_commit is None else git_commit
    timestamp = datetime.now(UTC) if created_at is None else created_at
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("backup creation time must include a timezone")
    backup_root.mkdir(parents=True, exist_ok=True)
    with _generation_lock(backup_root):
        generation_sequence = _next_generation_sequence(
            backup_root, authentication_key
        )
        staging = backup_root / f".backup-staging-{uuid4().hex}"
        generation = backup_root / _generation_name(timestamp, commit, uuid4().hex)
        staging.mkdir()
        try:
            artifact = staging / ARTIFACT_FILENAME
            create_sqlite_snapshot(runtime_paths.sqlite_path, artifact)
            verification = verify_sqlite_database(artifact)
            metadata: dict[str, JsonValue] = {
                "formatVersion": FORMAT_VERSION,
                "environmentId": runtime_paths.environment_id,
                "gitCommit": commit,
                "schemaVersion": verification.schema_version,
                "createdAt": timestamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "generationSequence": generation_sequence,
                "sqliteValidation": verification.integrity_check,
                "conversationCount": verification.conversation_count,
                "artifactSha256": sha256_file(artifact),
            }
            write_contract_files(staging, metadata, authentication_key)
            verify_backup(
                backup_directory=staging,
                authentication_key=authentication_key,
            )
            _fsync_file(artifact)
            _fsync_file(staging / METADATA_FILENAME)
            _fsync_file(staging / MANIFEST_FILENAME)
            _fsync_directory(staging)
            staging.rename(generation)
        except Exception:
            shutil.rmtree(staging)
            raise
        _fsync_directory(backup_root)
        _prune_backup_generations(
            backup_root, retention_count, authentication_key
        )
    return generation


def verify_backup(
    *, backup_directory: Path, authentication_key: BackupAuthenticationKey
) -> BackupVerification:
    _metadata, artifact = read_verified_generation(
        backup_directory, authentication_key
    )
    verification = verify_sqlite_database(artifact)
    verified_generation(backup_directory, verification, authentication_key)
    return verification


def restore_backup(
    *,
    runtime_paths: RuntimePaths,
    repository_root: Path,
    backup_directory: Path,
    authentication_key: BackupAuthenticationKey,
) -> BackupVerification:
    validate_existing_runtime_data_root(runtime_paths, repository_root)
    generation = _preflight(backup_directory, authentication_key)
    if generation.environment_id != runtime_paths.environment_id:
        raise BackupIdentityError("backup environment identity does not match destination")
    _reject_destination_with_sqlite_sidecars(runtime_paths.sqlite_path)
    staging = runtime_paths.data_root / f".{ARTIFACT_FILENAME}.staging-{uuid4().hex}"
    try:
        shutil.copyfile(generation.artifact_path, staging)
        if sha256_file(staging) != generation.artifact_sha256:
            raise BackupArtifactError("copied backup artifact checksum is invalid")
        result = verify_sqlite_database(staging)
        if result != generation.verification:
            raise BackupArtifactError("copied backup validation does not match backup")
        _fsync_file(staging)
        os.replace(staging, runtime_paths.sqlite_path)
        _fsync_directory(runtime_paths.data_root)
    except Exception as error:
        _remove_sqlite_staging_files(staging)
        if isinstance(error, (BackupArtifactError, BackupIdentityError)):
            raise
        from app.backup_restore.models import BackupSchemaError

        if isinstance(error, BackupSchemaError):
            raise
        raise RestoreSafetyError("restore database replacement failed safely") from error
    return result


def _remove_sqlite_staging_files(staging: Path) -> None:
    _remove_sqlite_database_files(staging)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as file:
        os.fsync(file.fileno())


def _fsync_directory(path: Path) -> None:
    file_descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)


def _reject_destination_with_sqlite_sidecars(database: Path) -> None:
    if any(
        os.path.lexists(database.with_name(database.name + suffix))
        for suffix in SQLITE_SIDECAR_SUFFIXES
    ):
        raise RestoreSafetyError(
            "restore destination contains SQLite sidecar files"
        )


def _remove_sqlite_database_files(database: Path) -> None:
    for suffix in ("", *SQLITE_SIDECAR_SUFFIXES):
        database.with_name(database.name + suffix).unlink(missing_ok=True)


def verify_restored_backup(
    *,
    runtime_paths: RuntimePaths,
    repository_root: Path,
    backup_directory: Path,
    authentication_key: BackupAuthenticationKey,
) -> BackupVerification:
    validate_existing_runtime_data_root(runtime_paths, repository_root)
    generation = _preflight(backup_directory, authentication_key)
    if generation.environment_id != runtime_paths.environment_id:
        raise BackupIdentityError("backup environment identity does not match destination")
    verification = verify_sqlite_database(runtime_paths.sqlite_path)
    if verification != generation.verification:
        raise BackupArtifactError("restored database validation does not match backup")
    return verification


def _prune_backup_generations(
    backup_root: Path,
    retention_count: int,
    authentication_key: BackupAuthenticationKey,
) -> tuple[Path, ...]:
    generations = sorted(
        _verified_generations(backup_root, authentication_key),
        key=lambda generation: generation.generation_sequence,
    )
    removed = tuple(generations[:-retention_count])
    for generation in removed:
        shutil.rmtree(generation.directory)
    return tuple(generation.directory for generation in removed)


@contextmanager
def _generation_lock(backup_root: Path) -> Iterator[None]:
    lock_path = backup_root / GENERATION_LOCK_FILENAME
    with lock_path.open("a", encoding="utf-8") as lock:
        flock(lock.fileno(), LOCK_EX)
        try:
            yield
        finally:
            flock(lock.fileno(), LOCK_UN)


def _preflight(
    backup_directory: Path, authentication_key: BackupAuthenticationKey
) -> VerifiedGeneration:
    _metadata, artifact = read_verified_generation(
        backup_directory, authentication_key
    )
    return verified_generation(
        backup_directory,
        verify_sqlite_database(artifact),
        authentication_key,
    )


def _validate_backup_root(backup_root: Path, data_root: Path) -> None:
    normalized_backup = backup_root.resolve(strict=False)
    normalized_data = data_root.resolve(strict=False)
    if normalized_backup == normalized_data or normalized_backup.is_relative_to(
        normalized_data
    ) or normalized_data.is_relative_to(normalized_backup):
        raise ValueError("backup root and runtime data root must not overlap")


def _generation_name(timestamp: datetime, commit: str, unique_id: str) -> str:
    utc_value = timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{GENERATION_PREFIX}{utc_value}-{commit[:12]}-{unique_id[:12]}"


def _next_generation_sequence(
    backup_root: Path, authentication_key: BackupAuthenticationKey
) -> int:
    existing = _verified_generations(backup_root, authentication_key)
    next_existing = max(
        (generation.generation_sequence for generation in existing),
        default=0,
    ) + 1
    return max(time_ns(), next_existing)


def _verified_generations(
    backup_root: Path, authentication_key: BackupAuthenticationKey
) -> tuple[VerifiedGeneration, ...]:
    generations: list[VerifiedGeneration] = []
    for path in backup_root.iterdir():
        if path.is_symlink() or not path.is_dir() or not path.name.startswith(
            GENERATION_PREFIX
        ):
            continue
        try:
            generations.append(_preflight(path, authentication_key))
        except (BackupError, OSError):
            continue
    return tuple(generations)


def _git_commit(repository_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise BackupArtifactError("Git commit could not be resolved")
    return commit

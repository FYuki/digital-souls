from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from time import time_ns
from collections.abc import Iterable
from typing import Iterator, cast
from uuid import uuid4

from app.backup_restore.contracts import (
    JsonValue,
    read_verified_generation,
    sha256_file,
    verified_generation,
    write_contract_files,
)
from app.backup_restore.models import (
    ARTIFACT_FILENAMES,
    CONVERSATION_ARTIFACT_FILENAME,
    FORMAT_VERSION,
    GENERATION_PREFIX,
    MANIFEST_FILENAME,
    METADATA_FILENAME,
    PERSONA_MEMORY_ARTIFACT_FILENAME,
    BackupAuthenticationKey,
    BackupArtifactError,
    BackupError,
    BackupIdentityError,
    BackupPublicationUncertainError,
    BackupVerification,
    BackupVerificationSet,
    RestoreDurabilityUncertainError,
    RestoreRecoveryRequiredError,
    RestoreSafetyError,
    VerifiedArtifact,
    VerifiedGeneration,
)
from app.backup_restore.sqlite_snapshot import (
    create_sqlite_snapshot,
    verify_sqlite_database,
)
from app.backup_restore.sqlite_sidecars import (
    remove_replaced_sqlite_sidecars,
    validate_sqlite_sidecars_for_restore,
)
from app.conversation_history.sqlite_lease import (
    SQLiteLease,
    SQLiteLeaseUnavailableError,
    acquire_maintenance_lease,
    normal_sqlite_access,
)
from app.memory.persistence.schema import initialize_persona_memory_schema
from app.runtime_data_root import validate_existing_runtime_data_root
from app.runtime_paths import RuntimePaths
from app.restore_intent import (
    RestoreIntent,
    complete_restore_intent,
    intent_for_generation,
    persist_restore_intent,
    read_restore_intent,
    restore_intent_exists,
)

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
    with normal_sqlite_access(runtime_paths.sqlite_path), normal_sqlite_access(
        runtime_paths.persona_memory_sqlite_path
    ):
        _validate_backup_root(backup_root, runtime_paths.data_root)
        if retention_count <= 0:
            raise ValueError("retention count must be positive")
        commit = _git_commit(repository_root) if git_commit is None else git_commit
        timestamp = datetime.now(UTC) if created_at is None else created_at
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("backup creation time must include a timezone")
        backup_root.mkdir(parents=True, exist_ok=True)
        with _generation_lock(backup_root):
            if not runtime_paths.persona_memory_sqlite_path.exists():
                initialize_persona_memory_schema(runtime_paths, repository_root)
            existing_generations = _verified_generations(
                backup_root, authentication_key
            )
            generation_sequence = _next_generation_sequence(existing_generations)
            staging = backup_root / f".backup-staging-{uuid4().hex}"
            generation = backup_root / _generation_name(timestamp, commit, uuid4().hex)
            staging.mkdir()
            try:
                source_paths = (
                    runtime_paths.sqlite_path,
                    runtime_paths.persona_memory_sqlite_path,
                )
                verifications: list[BackupVerification] = []
                artifact_metadata: list[JsonValue] = []
                for filename, source_path in zip(
                    ARTIFACT_FILENAMES, source_paths, strict=True
                ):
                    artifact = staging / filename
                    create_sqlite_snapshot(source_path, artifact)
                    verification = verify_sqlite_database(artifact, filename)
                    verifications.append(verification)
                    artifact_metadata.append(
                        {
                            "filename": filename,
                            "schemaVersion": verification.schema_version,
                            "sha256": sha256_file(artifact),
                            "integrityCheck": verification.integrity_check,
                            "requiredTables": cast(
                                list[JsonValue],
                                sorted(verification.required_tables),
                            ),
                            "recordCount": verification.record_count,
                        }
                    )
                metadata: dict[str, JsonValue] = {
                    "formatVersion": FORMAT_VERSION,
                    "environmentId": runtime_paths.environment_id,
                    "gitCommit": commit,
                    "createdAt": timestamp.astimezone(UTC).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "generationSequence": generation_sequence,
                    "artifacts": artifact_metadata,
                }
                write_contract_files(staging, metadata, authentication_key)
                verified_metadata, verified_artifacts = read_verified_generation(
                    staging, authentication_key
                )
                staged_generation = verified_generation(
                    staging,
                    verified_metadata,
                    verified_artifacts,
                    tuple(verifications),
                )
                for artifact in verified_artifacts:
                    _fsync_file(artifact)
                _fsync_file(staging / METADATA_FILENAME)
                _fsync_file(staging / MANIFEST_FILENAME)
                _fsync_directory(staging)
                staging.rename(generation)
            except Exception:
                with suppress(OSError):
                    shutil.rmtree(staging)
                raise
            try:
                _fsync_directory(backup_root)
            except OSError as error:
                raise BackupPublicationUncertainError(
                    "backup publication durability is uncertain"
                ) from error
            published_generation = _revalidate_generation(
                staged_generation, generation, authentication_key
            )
            if published_generation is None:
                raise BackupArtifactError(
                    "published backup generation verification failed"
                )
            _prune_backup_generations(
                (*existing_generations, published_generation),
                retention_count,
                authentication_key,
            )
        return generation


def verify_backup(
    *, backup_directory: Path, authentication_key: BackupAuthenticationKey
) -> BackupVerificationSet:
    return _preflight(backup_directory, authentication_key).verification


def restore_backup(
    *,
    runtime_paths: RuntimePaths,
    repository_root: Path,
    backup_directory: Path,
    authentication_key: BackupAuthenticationKey,
    maintenance_lease: SQLiteLease | None = None,
) -> BackupVerificationSet:
    try:
        validate_existing_runtime_data_root(runtime_paths, repository_root)
    except ValueError as error:
        if runtime_paths.restore_intent_path.is_symlink():
            raise RestoreRecoveryRequiredError(
                RestoreRecoveryRequiredError.public_message
            ) from error
        raise
    marker_existed = restore_intent_exists(runtime_paths.restore_intent_path)
    generation = None
    if not marker_existed:
        generation = _preflight(backup_directory, authentication_key)
        _require_destination_identity(runtime_paths, generation)
    try:
        lease_context = _restore_maintenance_leases(runtime_paths, maintenance_lease)
        with lease_context:
            marker_exists_under_lease = restore_intent_exists(
                runtime_paths.restore_intent_path
            )
            if marker_exists_under_lease:
                intent = read_restore_intent(runtime_paths.restore_intent_path)
                generation = _preflight(backup_directory, authentication_key)
                try:
                    _require_destination_identity(runtime_paths, generation)
                except BackupIdentityError as error:
                    raise RestoreRecoveryRequiredError(
                        RestoreRecoveryRequiredError.public_message
                    ) from error
                _require_matching_recovery(runtime_paths, generation, intent)
                return _replace_database(runtime_paths, generation, intent)
            if marker_existed or generation is None:
                raise RestoreRecoveryRequiredError(
                    RestoreRecoveryRequiredError.public_message
                )
            return _replace_database(runtime_paths, generation, None)
    except SQLiteLeaseUnavailableError as error:
        raise RestoreSafetyError("restore rejected while SQLite is in use") from error


def _replace_database(
    runtime_paths: RuntimePaths,
    generation: VerifiedGeneration,
    existing_intent: RestoreIntent | None,
) -> BackupVerificationSet:
    staging = {
        artifact.filename: runtime_paths.data_root
        / f".{artifact.filename}.staging-{uuid4().hex}"
        for artifact in generation.artifacts
    }
    try:
        pending_artifacts = (
            generation.artifacts
            if existing_intent is None
            else tuple(
                artifact
                for artifact in generation.artifacts
                if not _database_matches_artifact(
                    _database_path(runtime_paths, artifact.filename), artifact.sha256
                )
            )
        )
        _prepare_restore_staging(pending_artifacts, staging)
        if existing_intent is None:
            for database in _database_paths(runtime_paths):
                validate_sqlite_sidecars_for_restore(database)
        sqlite_before = {
            database: _sqlite_asset_snapshot(database)
            for database in _database_paths(runtime_paths)
        }
        if existing_intent is None:
            active_intent = intent_for_generation(
                runtime_paths.environment_id, generation
            )
            persist_restore_intent(runtime_paths.restore_intent_path, active_intent)
            replace_failure_cleanup_intent = active_intent
        else:
            active_intent = existing_intent
            replace_failure_cleanup_intent = None
    except Exception as error:
        _remove_sqlite_staging_files(staging.values())
        if isinstance(error, BackupError):
            raise
        raise RestoreSafetyError("restore preparation failed safely") from error
    return _commit_restore(
        runtime_paths,
        generation,
        staging,
        pending_artifacts,
        active_intent,
        replace_failure_cleanup_intent,
        sqlite_before,
    )


def _prepare_restore_staging(
    artifacts: tuple[VerifiedArtifact, ...],
    staging: dict[str, Path],
) -> None:
    for artifact in artifacts:
        staged_path = staging[artifact.filename]
        shutil.copyfile(artifact.path, staged_path)
        if sha256_file(staged_path) != artifact.sha256:
            raise BackupArtifactError("copied backup artifact checksum is invalid")
        result = verify_sqlite_database(staged_path, artifact.filename)
        if result != artifact.verification:
            raise BackupArtifactError("copied backup validation does not match backup")
        _fsync_file(staged_path)


def _commit_restore(
    runtime_paths: RuntimePaths,
    generation: VerifiedGeneration,
    staging: dict[str, Path],
    pending_artifacts: tuple[VerifiedArtifact, ...],
    intent: RestoreIntent,
    replace_failure_cleanup_intent: RestoreIntent | None,
    sqlite_before: dict[Path, dict[str, tuple[int, int, int, int, int] | None]],
) -> BackupVerificationSet:
    try:
        for artifact in pending_artifacts:
            os.replace(
                staging[artifact.filename],
                _database_path(runtime_paths, artifact.filename),
            )
    except OSError as error:
        _remove_sqlite_staging_files(staging.values())
        changed = any(
            _sqlite_asset_snapshot(database) != before
            for database, before in sqlite_before.items()
        )
        if changed:
            raise RestoreRecoveryRequiredError(
                RestoreRecoveryRequiredError.public_message
            ) from error
        if replace_failure_cleanup_intent is not None:
            complete_restore_intent(
                runtime_paths.restore_intent_path, replace_failure_cleanup_intent
            )
        raise RestoreSafetyError("restore database replacement failed safely") from error
    try:
        for database in _database_paths(runtime_paths):
            remove_replaced_sqlite_sidecars(database)
        _fsync_directory(runtime_paths.data_root)
        for artifact in generation.artifacts:
            database = _database_path(runtime_paths, artifact.filename)
            result = verify_sqlite_database(database, artifact.filename)
            if result != artifact.verification or sha256_file(database) != artifact.sha256:
                raise BackupArtifactError(
                    "restored database validation does not match backup"
                )
        complete_restore_intent(runtime_paths.restore_intent_path, intent)
        return generation.verification
    except RestoreDurabilityUncertainError:
        raise
    except Exception as error:
        raise RestoreDurabilityUncertainError(
            RestoreDurabilityUncertainError.public_message
        ) from error


def _sqlite_asset_snapshot(
    database: Path,
) -> dict[str, tuple[int, int, int, int, int] | None]:
    return {
        suffix: _sqlite_asset_identity(path)
        for suffix in ("", "-wal", "-shm", "-journal")
        for path in (database.with_name(database.name + suffix),)
    }


def _sqlite_asset_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        status = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (
        status.st_mode,
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
    )


def _require_destination_identity(
    runtime_paths: RuntimePaths, generation: VerifiedGeneration
) -> None:
    if generation.environment_id != runtime_paths.environment_id:
        raise BackupIdentityError("backup environment identity does not match destination")


def _require_matching_recovery(
    runtime_paths: RuntimePaths,
    generation: VerifiedGeneration,
    intent: RestoreIntent,
) -> None:
    expected = intent_for_generation(runtime_paths.environment_id, generation)
    if intent != expected:
        raise RestoreRecoveryRequiredError(
            RestoreRecoveryRequiredError.public_message
        )


@contextmanager
def _restore_maintenance_leases(
    runtime_paths: RuntimePaths, existing: SQLiteLease | None
) -> Iterator[tuple[SQLiteLease, SQLiteLease]]:
    if existing is not None:
        existing.require_maintenance_for(runtime_paths.sqlite_path)
        with acquire_maintenance_lease(
            runtime_paths.persona_memory_sqlite_path
        ) as persona_lease:
            yield existing, persona_lease
        return
    with acquire_maintenance_lease(runtime_paths.sqlite_path) as conversation_lease:
        with acquire_maintenance_lease(
            runtime_paths.persona_memory_sqlite_path
        ) as persona_lease:
            yield conversation_lease, persona_lease


def _remove_sqlite_staging_files(staging_paths: Iterable[Path]) -> None:
    for staging in staging_paths:
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


def _remove_sqlite_database_files(database: Path) -> None:
    for suffix in ("", *SQLITE_SIDECAR_SUFFIXES):
        database.with_name(database.name + suffix).unlink(missing_ok=True)


def verify_restored_backup(
    *,
    runtime_paths: RuntimePaths,
    repository_root: Path,
    backup_directory: Path,
    authentication_key: BackupAuthenticationKey,
) -> BackupVerificationSet:
    validate_existing_runtime_data_root(runtime_paths, repository_root)
    with normal_sqlite_access(runtime_paths.sqlite_path), normal_sqlite_access(
        runtime_paths.persona_memory_sqlite_path
    ):
        generation = _preflight(backup_directory, authentication_key)
        if generation.environment_id != runtime_paths.environment_id:
            raise BackupIdentityError(
                "backup environment identity does not match destination"
            )
        verifications = tuple(
            verify_sqlite_database(
                _database_path(runtime_paths, artifact.filename), artifact.filename
            )
            for artifact in generation.artifacts
        )
        if verifications != generation.verification.artifacts:
            raise BackupArtifactError("restored database validation does not match backup")
        return generation.verification


def _prune_backup_generations(
    generations: tuple[VerifiedGeneration, ...],
    retention_count: int,
    authentication_key: BackupAuthenticationKey,
) -> tuple[Path, ...]:
    sorted_generations = sorted(
        generations,
        key=lambda generation: generation.generation_sequence,
    )
    candidates = tuple(sorted_generations[:-retention_count])
    removed: list[Path] = []
    for candidate in candidates:
        verified_candidate = _revalidate_generation(
            candidate, candidate.directory, authentication_key
        )
        if verified_candidate is None:
            continue
        shutil.rmtree(verified_candidate.directory)
        removed.append(verified_candidate.directory)
    return tuple(removed)


def _revalidate_generation(
    expected: VerifiedGeneration,
    directory: Path,
    authentication_key: BackupAuthenticationKey,
) -> VerifiedGeneration | None:
    try:
        actual = _preflight(directory, authentication_key)
    except (BackupError, OSError):
        return None
    if actual.generation_identity_sha256 != expected.generation_identity_sha256:
        return None
    return actual


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
    metadata, artifacts = read_verified_generation(
        backup_directory, authentication_key
    )
    verifications = tuple(
        verify_sqlite_database(artifact, filename)
        for filename, artifact in zip(ARTIFACT_FILENAMES, artifacts, strict=True)
    )
    return verified_generation(
        backup_directory,
        metadata,
        artifacts,
        verifications,
    )


def _database_paths(runtime_paths: RuntimePaths) -> tuple[Path, Path]:
    return runtime_paths.sqlite_path, runtime_paths.persona_memory_sqlite_path


def _database_path(runtime_paths: RuntimePaths, filename: str) -> Path:
    if filename == CONVERSATION_ARTIFACT_FILENAME:
        return runtime_paths.sqlite_path
    if filename == PERSONA_MEMORY_ARTIFACT_FILENAME:
        return runtime_paths.persona_memory_sqlite_path
    raise BackupArtifactError("backup artifact filename is unsupported")


def _database_matches_artifact(database: Path, expected_sha256: str) -> bool:
    try:
        return sha256_file(database) == expected_sha256
    except FileNotFoundError:
        return False


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
    existing: tuple[VerifiedGeneration, ...],
) -> int:
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

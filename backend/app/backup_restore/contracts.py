from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import TypeAlias, cast

from app.backup_restore.models import (
    ARTIFACT_FILENAME,
    FORMAT_VERSION,
    MANIFEST_FILENAME,
    METADATA_FILENAME,
    BackupAuthenticationKey,
    BackupArtifactError,
    BackupVerification,
    VerifiedGeneration,
)
from app.runtime_paths import SUPPORTED_ENVIRONMENT_IDS

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
METADATA_FIELDS = frozenset(
    {
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
)
MANIFEST_FIELDS = frozenset(
    {"formatVersion", "complete", "files", "authenticationHmacSha256"}
)
UNSIGNED_MANIFEST_FIELDS = frozenset({"formatVersion", "complete", "files"})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_contract_files(
    directory: Path,
    metadata: dict[str, JsonValue],
    authentication_key: BackupAuthenticationKey,
) -> None:
    metadata_path = directory / METADATA_FILENAME
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    files: list[JsonValue] = [
        {"path": name, "sha256": sha256_file(directory / name)}
        for name in (ARTIFACT_FILENAME, METADATA_FILENAME)
    ]
    unsigned_manifest: dict[str, JsonValue] = {
        "formatVersion": FORMAT_VERSION,
        "complete": True,
        "files": files,
    }
    manifest: dict[str, JsonValue] = {
        **unsigned_manifest,
        "authenticationHmacSha256": _authentication_hmac(
            directory,
            metadata,
            unsigned_manifest,
            authentication_key,
        ),
    }
    (directory / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def read_verified_generation(
    directory: Path, authentication_key: BackupAuthenticationKey
) -> tuple[dict[str, JsonValue], Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise BackupArtifactError("backup artifact directory is invalid")
    metadata = _read_object(directory / METADATA_FILENAME, "metadata")
    manifest = _read_object(directory / MANIFEST_FILENAME, "manifest")
    _validate_metadata(metadata)
    _validate_manifest(directory, metadata, manifest, authentication_key)
    artifact = directory / ARTIFACT_FILENAME
    if metadata["artifactSha256"] != sha256_file(artifact):
        raise BackupArtifactError("artifact checksum does not match metadata")
    return metadata, artifact


def verified_generation(
    directory: Path,
    verification: BackupVerification,
    authentication_key: BackupAuthenticationKey,
) -> VerifiedGeneration:
    metadata, artifact = read_verified_generation(directory, authentication_key)
    if metadata["schemaVersion"] != verification.schema_version:
        from app.backup_restore.models import BackupSchemaError

        raise BackupSchemaError("backup schema metadata does not match artifact")
    if metadata["conversationCount"] != verification.conversation_count:
        raise BackupArtifactError("backup metadata validation values do not match")
    return VerifiedGeneration(
        directory=directory,
        environment_id=str(metadata["environmentId"]),
        generation_sequence=cast(int, metadata["generationSequence"]),
        artifact_path=artifact,
        artifact_sha256=str(metadata["artifactSha256"]),
        verification=verification,
    )


def _read_object(path: Path, label: str) -> dict[str, JsonValue]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupArtifactError(f"backup {label} is missing or invalid") from error
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise BackupArtifactError(f"backup {label} is invalid")
    return cast(dict[str, JsonValue], raw)


def _validate_metadata(metadata: dict[str, JsonValue]) -> None:
    if set(metadata) != METADATA_FIELDS:
        raise BackupArtifactError("backup metadata fields are invalid")
    if metadata["formatVersion"] != FORMAT_VERSION:
        raise BackupArtifactError("backup metadata format is unsupported")
    string_fields = ("environmentId", "gitCommit", "createdAt", "sqliteValidation")
    if any(not isinstance(metadata[field], str) for field in string_fields):
        raise BackupArtifactError("backup metadata value types are invalid")
    integer_fields = ("schemaVersion", "conversationCount", "generationSequence")
    if any(
        isinstance(metadata[field], bool) or not isinstance(metadata[field], int)
        for field in integer_fields
    ):
        raise BackupArtifactError("backup metadata value types are invalid")
    checksum = metadata["artifactSha256"]
    commit = metadata["gitCommit"]
    created_at = metadata["createdAt"]
    if metadata["environmentId"] not in SUPPORTED_ENVIRONMENT_IDS:
        raise BackupArtifactError("backup metadata environment is invalid")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise BackupArtifactError("backup metadata commit is invalid")
    if not isinstance(created_at, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        created_at,
    ) is None:
        raise BackupArtifactError("backup metadata creation time is invalid")
    if metadata["sqliteValidation"] != "ok":
        raise BackupArtifactError("backup metadata SQLite validation is invalid")
    conversation_count = metadata["conversationCount"]
    if (
        isinstance(conversation_count, bool)
        or not isinstance(conversation_count, int)
        or conversation_count < 0
    ):
        raise BackupArtifactError("backup metadata conversation count is invalid")
    generation_sequence = metadata["generationSequence"]
    if (
        isinstance(generation_sequence, bool)
        or not isinstance(generation_sequence, int)
        or generation_sequence <= 0
    ):
        raise BackupArtifactError("backup metadata generation sequence is invalid")
    if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise BackupArtifactError("backup metadata checksum is invalid")


def _validate_manifest(
    directory: Path,
    metadata: dict[str, JsonValue],
    manifest: dict[str, JsonValue],
    authentication_key: BackupAuthenticationKey,
) -> None:
    if set(manifest) != MANIFEST_FIELDS:
        raise BackupArtifactError("backup manifest fields are invalid")
    if manifest["formatVersion"] != FORMAT_VERSION or manifest["complete"] is not True:
        raise BackupArtifactError("backup manifest is incomplete or unsupported")
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != 2:
        raise BackupArtifactError("backup manifest file list is invalid")
    expected = (ARTIFACT_FILENAME, METADATA_FILENAME)
    for entry, expected_name in zip(files, expected, strict=True):
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise BackupArtifactError("backup manifest entry is invalid")
        try:
            actual_checksum = sha256_file(directory / expected_name)
        except OSError as error:
            raise BackupArtifactError("backup manifest file is missing") from error
        if entry["path"] != expected_name or entry["sha256"] != actual_checksum:
            raise BackupArtifactError("backup manifest checksum is invalid")
    authentication_hmac = manifest["authenticationHmacSha256"]
    if not isinstance(authentication_hmac, str) or re.fullmatch(
        r"[0-9a-f]{64}", authentication_hmac
    ) is None:
        raise BackupArtifactError("backup manifest authentication is invalid")
    unsigned_manifest = {
        key: value for key, value in manifest.items() if key in UNSIGNED_MANIFEST_FIELDS
    }
    expected_hmac = _authentication_hmac(
        directory,
        metadata,
        unsigned_manifest,
        authentication_key,
    )
    if not hmac.compare_digest(authentication_hmac, expected_hmac):
        raise BackupArtifactError("backup manifest authentication is invalid")


def _authentication_hmac(
    directory: Path,
    metadata: dict[str, JsonValue],
    unsigned_manifest: dict[str, JsonValue],
    authentication_key: BackupAuthenticationKey,
) -> str:
    digest = hmac.new(authentication_key.value, digestmod=hashlib.sha256)
    digest.update(b"digital-souls-backup-v2\0artifact\0")
    with (directory / ARTIFACT_FILENAME).open("rb") as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(block)
    digest.update(b"\0metadata\0")
    digest.update(_canonical_json(metadata))
    digest.update(b"\0manifest\0")
    digest.update(_canonical_json(unsigned_manifest))
    return digest.hexdigest()


def _canonical_json(value: dict[str, JsonValue]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

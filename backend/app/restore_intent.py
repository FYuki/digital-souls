from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from app.backup_restore.models import (
    ARTIFACT_FILENAMES,
    RestoreDurabilityUncertainError,
    RestoreRecoveryRequiredError,
    VerifiedGeneration,
)
from app.runtime_paths import RESTORE_INTENT_FILENAME, SUPPORTED_ENVIRONMENT_IDS

RESTORE_INTENT_FORMAT_VERSION = 1
RESTORE_INTENT_FIELDS = frozenset(
    {
        "formatVersion",
        "environmentId",
        "generationSequence",
        "artifacts",
        "generationIdentitySha256",
    }
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class RestoreIntentArtifact:
    filename: str
    sha256: str


@dataclass(frozen=True)
class RestoreIntent:
    formatVersion: int
    environmentId: str
    generationSequence: int
    artifacts: tuple[RestoreIntentArtifact, ...]
    generationIdentitySha256: str


def intent_for_generation(
    environment_id: str, generation: VerifiedGeneration
) -> RestoreIntent:
    return RestoreIntent(
        formatVersion=RESTORE_INTENT_FORMAT_VERSION,
        environmentId=environment_id,
        generationSequence=generation.generation_sequence,
        artifacts=tuple(
            RestoreIntentArtifact(artifact.filename, artifact.sha256)
            for artifact in generation.artifacts
        ),
        generationIdentitySha256=generation.generation_identity_sha256,
    )


def restore_intent_path_for_database(database_path: Path) -> Path:
    return database_path.parent / RESTORE_INTENT_FILENAME


def restore_intent_exists(marker_path: Path) -> bool:
    return os.path.lexists(marker_path)


def require_no_restore_intent(marker_path: Path) -> None:
    if restore_intent_exists(marker_path):
        raise RestoreRecoveryRequiredError(
            RestoreRecoveryRequiredError.public_message
        )


def require_sqlite_available(database_path: Path) -> None:
    require_no_restore_intent(restore_intent_path_for_database(database_path))


def read_restore_intent(marker_path: Path) -> RestoreIntent:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        descriptor = os.open(marker_path, flags)
        try:
            marker_file = os.fdopen(descriptor, "r", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with marker_file:
            if not stat.S_ISREG(os.fstat(marker_file.fileno()).st_mode):
                raise RestoreRecoveryRequiredError(
                    RestoreRecoveryRequiredError.public_message
                )
            raw: object = json.load(marker_file)
    except RestoreRecoveryRequiredError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RestoreRecoveryRequiredError(
            RestoreRecoveryRequiredError.public_message
        ) from error
    return _parse_restore_intent(raw)


def persist_restore_intent(marker_path: Path, intent: RestoreIntent) -> None:
    payload = (
        json.dumps(asdict(intent), ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(marker_path, flags, 0o600)
    except FileExistsError as error:
        raise RestoreRecoveryRequiredError(
            RestoreRecoveryRequiredError.public_message
        ) from error
    try:
        try:
            marker_file = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with marker_file:
            os.fchmod(marker_file.fileno(), 0o600)
            marker_file.write(payload)
            marker_file.flush()
            os.fsync(marker_file.fileno())
        fsync_directory(marker_path.parent)
    except OSError as error:
        raise RestoreDurabilityUncertainError(
            RestoreDurabilityUncertainError.public_message
        ) from error


def complete_restore_intent(marker_path: Path, intent: RestoreIntent) -> None:
    try:
        marker_path.unlink()
        fsync_directory(marker_path.parent)
    except OSError as error:
        if not restore_intent_exists(marker_path):
            try:
                persist_restore_intent(marker_path, intent)
            except Exception as reestablish_error:
                raise RestoreDurabilityUncertainError(
                    RestoreDurabilityUncertainError.public_message
                ) from reestablish_error
        raise RestoreDurabilityUncertainError(
            RestoreDurabilityUncertainError.public_message
        ) from error


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_restore_intent(raw: object) -> RestoreIntent:
    if not isinstance(raw, dict) or set(raw) != RESTORE_INTENT_FIELDS:
        raise RestoreRecoveryRequiredError(
            RestoreRecoveryRequiredError.public_message
        )
    values = cast(dict[str, object], raw)
    format_version = values["formatVersion"]
    environment_id = values["environmentId"]
    generation_sequence = values["generationSequence"]
    raw_artifacts = values["artifacts"]
    generation_identity_sha256 = values["generationIdentitySha256"]
    if (
        isinstance(format_version, bool)
        or not isinstance(format_version, int)
        or format_version != RESTORE_INTENT_FORMAT_VERSION
    ):
        raise RestoreRecoveryRequiredError(
            RestoreRecoveryRequiredError.public_message
        )
    if (
        not isinstance(environment_id, str)
        or environment_id not in SUPPORTED_ENVIRONMENT_IDS
    ):
        raise RestoreRecoveryRequiredError(
            RestoreRecoveryRequiredError.public_message
        )
    if (
        isinstance(generation_sequence, bool)
        or not isinstance(generation_sequence, int)
        or generation_sequence <= 0
    ):
        raise RestoreRecoveryRequiredError(
            RestoreRecoveryRequiredError.public_message
        )
    if not isinstance(raw_artifacts, list) or len(raw_artifacts) != 2:
        raise RestoreRecoveryRequiredError(
            RestoreRecoveryRequiredError.public_message
        )
    artifacts: list[RestoreIntentArtifact] = []
    for raw_artifact, filename in zip(
        raw_artifacts, ARTIFACT_FILENAMES, strict=True
    ):
        if (
            not isinstance(raw_artifact, dict)
            or set(raw_artifact) != {"filename", "sha256"}
            or raw_artifact["filename"] != filename
            or not _is_sha256(raw_artifact["sha256"])
        ):
            raise RestoreRecoveryRequiredError(
                RestoreRecoveryRequiredError.public_message
            )
        artifacts.append(
            RestoreIntentArtifact(filename, cast(str, raw_artifact["sha256"]))
        )
    if not isinstance(generation_identity_sha256, str) or not _is_sha256(
        generation_identity_sha256
    ):
        raise RestoreRecoveryRequiredError(
            RestoreRecoveryRequiredError.public_message
        )
    return RestoreIntent(
        formatVersion=format_version,
        environmentId=environment_id,
        generationSequence=generation_sequence,
        artifacts=tuple(artifacts),
        generationIdentitySha256=generation_identity_sha256,
    )


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


FORMAT_VERSION = 2
ARTIFACT_FILENAME = "conversation-history.db"
METADATA_FILENAME = "metadata.json"
MANIFEST_FILENAME = "manifest.json"
GENERATION_PREFIX = "backup-"
BACKUP_AUTHENTICATION_KEY_ENV = "DOGFOOD_BACKUP_AUTHENTICATION_KEY"


class BackupError(RuntimeError):
    pass


class BackupIdentityError(BackupError):
    pass


class BackupArtifactError(BackupError):
    pass


class BackupSchemaError(BackupError):
    pass


class RestoreSafetyError(BackupError):
    pass


@dataclass(frozen=True, repr=False)
class BackupAuthenticationKey:
    value: bytes = field(repr=False)


def resolve_backup_authentication_key(
    environment: Mapping[str, str],
) -> BackupAuthenticationKey:
    encoded = environment.get(BACKUP_AUTHENTICATION_KEY_ENV)
    if encoded is None or re.fullmatch(r"[0-9a-fA-F]{64}", encoded) is None:
        raise ValueError("backup authentication key must be 64 hexadecimal characters")
    return BackupAuthenticationKey(bytes.fromhex(encoded))


@dataclass(frozen=True)
class BackupVerification:
    integrity_check: str
    schema_version: int
    required_tables: frozenset[str]
    conversation_count: int


@dataclass(frozen=True)
class VerifiedGeneration:
    directory: Path
    environment_id: str
    generation_sequence: int
    artifact_path: Path
    artifact_sha256: str
    verification: BackupVerification

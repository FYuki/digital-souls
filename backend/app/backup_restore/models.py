from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


FORMAT_VERSION = 3
CONVERSATION_ARTIFACT_FILENAME = "conversation-history.db"
PERSONA_MEMORY_ARTIFACT_FILENAME = "persona-memory.db"
ARTIFACT_FILENAMES = (
    CONVERSATION_ARTIFACT_FILENAME,
    PERSONA_MEMORY_ARTIFACT_FILENAME,
)
METADATA_FILENAME = "metadata.json"
MANIFEST_FILENAME = "manifest.json"
GENERATION_PREFIX = "backup-"
BACKUP_AUTHENTICATION_KEY_ENV = "DOGFOOD_BACKUP_AUTHENTICATION_KEY"


class BackupError(RuntimeError):
    public_message = "backup operation failed"


class BackupIdentityError(BackupError):
    public_message = "backup environment identity is invalid"


class BackupArtifactError(BackupError):
    public_message = "backup artifact is invalid"


class BackupSchemaError(BackupError):
    public_message = "backup schema is invalid"


class RestoreSafetyError(BackupError):
    public_message = "restore was rejected safely"


class BackupPublicationUncertainError(BackupError):
    public_message = "backup publication durability is uncertain"


class RestoreDurabilityUncertainError(BackupError):
    public_message = "restore durability is uncertain"


class RestoreRecoveryRequiredError(BackupError):
    public_message = "interrupted restore recovery is required"


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
    filename: str
    integrity_check: str
    schema_version: int
    required_tables: frozenset[str]
    record_count: int


@dataclass(frozen=True)
class BackupVerificationSet:
    artifacts: tuple[BackupVerification, ...]

    def artifact(self, filename: str) -> BackupVerification:
        for artifact in self.artifacts:
            if artifact.filename == filename:
                return artifact
        raise KeyError(filename)


@dataclass(frozen=True)
class VerifiedArtifact:
    filename: str
    path: Path
    sha256: str
    verification: BackupVerification


@dataclass(frozen=True)
class VerifiedGeneration:
    directory: Path
    environment_id: str
    generation_sequence: int
    artifacts: tuple[VerifiedArtifact, ...]
    generation_identity_sha256: str
    verification: BackupVerificationSet

from app.backup_restore.models import (
    BackupAuthenticationKey,
    BackupArtifactError,
    BackupIdentityError,
    BackupPublicationUncertainError,
    BackupSchemaError,
    BackupVerification,
    BackupVerificationSet,
    RestoreSafetyError,
    RestoreDurabilityUncertainError,
    RestoreRecoveryRequiredError,
    resolve_backup_authentication_key,
)
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from app.backup_restore.service import (
        create_backup,
        restore_backup,
        verify_backup,
        verify_restored_backup,
    )

__all__ = (
    "BackupArtifactError",
    "BackupAuthenticationKey",
    "BackupIdentityError",
    "BackupPublicationUncertainError",
    "BackupSchemaError",
    "BackupVerification",
    "BackupVerificationSet",
    "RestoreSafetyError",
    "RestoreDurabilityUncertainError",
    "RestoreRecoveryRequiredError",
    "create_backup",
    "resolve_backup_authentication_key",
    "restore_backup",
    "verify_backup",
    "verify_restored_backup",
)


def __getattr__(name: str) -> Callable[..., object]:
    if name not in {
        "create_backup",
        "restore_backup",
        "verify_backup",
        "verify_restored_backup",
    }:
        raise AttributeError(name)
    from app.backup_restore import service

    return cast(Callable[..., object], getattr(service, name))

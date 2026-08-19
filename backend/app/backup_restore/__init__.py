from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from app.backup_restore.service import (
        create_backup,
        restore_backup,
        verify_backup,
        verify_restored_backup,
    )

_SERVICE_EXPORTS = frozenset(
    {
        "create_backup",
        "restore_backup",
        "verify_backup",
        "verify_restored_backup",
    }
)


def __getattr__(name: str) -> object:
    if name not in _SERVICE_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from app.backup_restore import service

    operation = getattr(service, name)
    globals()[name] = operation
    return operation


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

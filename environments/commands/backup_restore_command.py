from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from app.backup_restore import (
    BackupVerificationSet,
    create_backup,
    resolve_backup_authentication_key,
    restore_backup,
    verify_backup,
    verify_restored_backup,
)
from app.runtime_data_root import initialize_runtime_data_root
from app.runtime_paths import DATA_DIR_ENV, ENVIRONMENT_ID_ENV, resolve_runtime_paths


def initialize_environment_data_root(
    environment_id: str, repository_root_value: str
) -> int:
    repository_root = Path(repository_root_value)
    paths = resolve_runtime_paths(
        _runtime_environment(environment_id), repository_root
    )
    initialize_runtime_data_root(paths, repository_root)
    _print_json(
        {
            "status": "ok",
            "environmentId": paths.environment_id,
            "dataRoot": str(paths.data_root),
        }
    )
    return 0


def backup_environment(
    environment_id: str,
    repository_root_value: str,
    backup_root_value: str,
    retention_count: int,
) -> int:
    authentication_key = resolve_backup_authentication_key(os.environ)
    repository_root = Path(repository_root_value)
    paths = resolve_runtime_paths(
        _runtime_environment(environment_id), repository_root
    )
    generation = create_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_root=Path(backup_root_value),
        retention_count=retention_count,
        authentication_key=authentication_key,
    )
    _print_json({"status": "ok", "backupDirectory": str(generation)})
    return 0


def verify_environment_backup(backup_directory_value: str) -> int:
    result = verify_backup(
        backup_directory=Path(backup_directory_value),
        authentication_key=resolve_backup_authentication_key(os.environ),
    )
    _print_verification(result)
    return 0


def restore_environment_backup(
    environment_id: str,
    repository_root_value: str,
    backup_directory_value: str,
) -> int:
    repository_root = Path(repository_root_value)
    authentication_key = resolve_backup_authentication_key(os.environ)
    result = restore_backup(
        runtime_paths=resolve_runtime_paths(
            _runtime_environment(environment_id), repository_root
        ),
        repository_root=repository_root,
        backup_directory=Path(backup_directory_value),
        authentication_key=authentication_key,
    )
    _print_verification(result)
    return 0


def verify_restored_environment_backup(
    environment_id: str,
    repository_root_value: str,
    backup_directory_value: str,
) -> int:
    repository_root = Path(repository_root_value)
    authentication_key = resolve_backup_authentication_key(os.environ)
    result = verify_restored_backup(
        runtime_paths=resolve_runtime_paths(
            _runtime_environment(environment_id), repository_root
        ),
        repository_root=repository_root,
        backup_directory=Path(backup_directory_value),
        authentication_key=authentication_key,
    )
    _print_verification(result)
    return 0


def _runtime_environment(environment_id: str) -> dict[str, str]:
    return {
        ENVIRONMENT_ID_ENV: environment_id,
        DATA_DIR_ENV: os.environ[DATA_DIR_ENV],
    }


def _print_verification(result: BackupVerificationSet) -> None:
    _print_json(
        {
            "status": "ok",
            "artifacts": [
                {
                    "filename": artifact.filename,
                    "schemaVersion": artifact.schema_version,
                    "recordCount": artifact.record_count,
                }
                for artifact in result.artifacts
            ],
        }
    )


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

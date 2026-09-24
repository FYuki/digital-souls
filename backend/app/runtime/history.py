"""会話履歴のschema・SQLite lease・Repository・Serviceを所有する。"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from app.backup_restore import (
    BackupAuthenticationKey,
    create_backup,
    resolve_backup_authentication_key,
    restore_backup,
    verify_backup,
    verify_restored_backup,
)
from app.conversation_history.config import (
    ConversationHistoryConfig,
    resolve_conversation_history_config,
)
from app.conversation_history.errors import SchemaRollbackError
from app.conversation_history.lifecycle_service import ConversationLifecycleService
from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.schema import (
    initialize_conversation_history_schema,
    inspect_conversation_history_schema,
)
from app.conversation_history.service import ConversationHistoryService
from app.conversation_history.sqlite_lease import SQLiteLease
from app.conversation_history.wal_cleanup import ConversationWalCleanup
from app.runtime_paths import RuntimePaths
from app.ui_settings import UiSettingsRepository

if TYPE_CHECKING:
    from fastapi import FastAPI
    from app.privacy.history_sanitizer import HistorySanitizer


DOGFOOD_BACKUP_DIR_ENV = "DOGFOOD_BACKUP_DIR"
DOGFOOD_BACKUP_RETENTION_COUNT_ENV = "DOGFOOD_BACKUP_RETENTION_COUNT"
DS_DEPLOYMENT_COMMIT_ENV = "DS_DEPLOYMENT_COMMIT"


@dataclass(frozen=True)
class _SchemaRollbackContext:
    generation: Path
    authentication_key: BackupAuthenticationKey


def ensure_schema_backup_gate(
    paths: RuntimePaths, repository_root: Path
) -> _SchemaRollbackContext | None:
    inspection = inspect_conversation_history_schema(paths.sqlite_path)
    if paths.environment_id != "dogfood" or not inspection.migration_required:
        return None
    backup_root_value = os.environ.get(DOGFOOD_BACKUP_DIR_ENV)
    retention_value = os.environ.get(DOGFOOD_BACKUP_RETENTION_COUNT_ENV)
    if not backup_root_value or retention_value is None:
        raise RuntimeError("dogfood schema backup configuration is required")
    try:
        retention_count = int(retention_value)
    except ValueError as error:
        raise RuntimeError("dogfood backup retention count is invalid") from error
    if retention_count <= 0:
        raise RuntimeError("dogfood backup retention count is invalid")
    deployment_commit = os.environ.get(DS_DEPLOYMENT_COMMIT_ENV)
    if (
        deployment_commit is not None
        and re.fullmatch(r"[0-9a-f]{40}", deployment_commit) is None
    ):
        raise RuntimeError("deployment commit is invalid")
    authentication_key = resolve_backup_authentication_key(os.environ)
    generation = create_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_root=Path(backup_root_value),
        retention_count=retention_count,
        authentication_key=authentication_key,
        git_commit=deployment_commit,
    )
    verify_backup(
        backup_directory=generation,
        authentication_key=authentication_key,
    )
    return _SchemaRollbackContext(generation, authentication_key)


def _initialize_schema_with_rollback(
    *,
    database_path: Path,
    runtime_paths: RuntimePaths,
    repository_root: Path,
    rollback: _SchemaRollbackContext | None,
    maintenance_lease: SQLiteLease | None = None,
) -> None:
    if rollback is None:
        initialize_conversation_history_schema(database_path)
        return
    try:
        initialize_conversation_history_schema(database_path)
    except Exception as primary_error:
        try:
            restore_backup(
                runtime_paths=runtime_paths,
                repository_root=repository_root,
                backup_directory=rollback.generation,
                authentication_key=rollback.authentication_key,
                maintenance_lease=maintenance_lease,
            )
        except Exception as compensation_error:
            raise SchemaRollbackError(
                primary_error, compensation_error, "restore"
            ) from None
        try:
            verify_restored_backup(
                runtime_paths=runtime_paths,
                repository_root=repository_root,
                backup_directory=rollback.generation,
                authentication_key=rollback.authentication_key,
            )
        except Exception as compensation_error:
            raise SchemaRollbackError(
                primary_error, compensation_error, "verification"
            ) from None
        raise


class HistoryResources:
    """会話履歴のlease・schema・Repository・管理Serviceを所有する資源owner。"""

    def __init__(self, config: ConversationHistoryConfig) -> None:
        self.config = config
        self.lease: SQLiteLease | None = None
        self.wal_cleanup: ConversationWalCleanup | None = None
        self.repository: ConversationHistoryRepository | None = None
        self.lifecycle_service: ConversationLifecycleService | None = None
        self.ui_settings_repository: UiSettingsRepository | None = None
        self.service: ConversationHistoryService | None = None
        self.repository_published = False
        self.lifecycle_published = False
        self.ui_settings_published = False

    @classmethod
    def resolve(cls, runtime_paths: RuntimePaths) -> HistoryResources:
        return cls(resolve_conversation_history_config(runtime_paths))

    def initialize_schema(
        self, runtime_paths: RuntimePaths, repository_root: Path
    ) -> None:
        assert self.lease is not None
        rollback = ensure_schema_backup_gate(runtime_paths, repository_root)
        _initialize_schema_with_rollback(
            database_path=self.config.database_path,
            runtime_paths=runtime_paths,
            repository_root=repository_root,
            rollback=rollback,
            maintenance_lease=self.lease,
        )
        self.lease.transition_to_runtime()

    def build(self, clock: Callable[[], datetime]) -> None:
        self.wal_cleanup = ConversationWalCleanup(
            database_path=self.config.database_path,
            clock=clock,
            connection_factory=sqlite3.connect,
        )
        self.repository = ConversationHistoryRepository(
            database_path=self.config.database_path,
            stale_after=self.config.stale_after,
            retention=self.config.retention,
            clock=clock,
            uuid_factory=uuid4,
            wal_cleanup=self.wal_cleanup,
        )
        self.repository.recover_stale_processing()
        self.wal_cleanup.retry_pending()
        self.lifecycle_service = ConversationLifecycleService(self.repository)
        self.ui_settings_repository = UiSettingsRepository(
            database_path=self.config.database_path,
            clock=clock,
        )

    def build_service(self, history_sanitizer: HistorySanitizer) -> None:
        assert self.repository is not None
        self.service = ConversationHistoryService(
            self.repository,
            history_sanitizer,
        )

    def publish_repository(self, app: FastAPI) -> None:
        app.state.conversation_history_repository = self.repository
        self.repository_published = True

    def publish_services(self, app: FastAPI) -> None:
        app.state.conversation_lifecycle_service = self.lifecycle_service
        self.lifecycle_published = True
        app.state.ui_settings_repository = self.ui_settings_repository
        self.ui_settings_published = True

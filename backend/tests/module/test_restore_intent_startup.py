from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import FastAPI

from app.runtime_paths import RESTORE_INTENT_FILENAME
from tests.backup_restore_test_support import (
    FIXED_BACKUP_TIME,
    FIXED_COMMIT,
    TEST_AUTHENTICATION_KEY,
    create_history_database,
    initialized_runtime,
)


PUBLIC_RECOVERY_MESSAGE = "interrupted restore recovery is required"


def _recovery_error_type():
    import app.backup_restore as backup_restore

    assert hasattr(backup_restore, "RestoreRecoveryRequiredError"), (
        "RESTORE-STARTUP-BLOCK-01 requires a typed recovery-required error"
    )
    return backup_restore.RestoreRecoveryRequiredError


def _place_marker(data_root: Path) -> Path:
    marker = data_root / RESTORE_INTENT_FILENAME
    marker.write_text(
        '{"privatePath":"/private/conversations/miori.db",'
        '"secret":"marker-secret-value"}\n',
        encoding="utf-8",
    )
    marker.chmod(0o600)
    return marker


def _create_generation(tmp_path: Path, repository_root: Path) -> Path:
    from app.backup_restore import create_backup

    source = initialized_runtime(tmp_path, repository_root, name="source")
    connection = create_history_database(source, wal=False)
    try:
        return create_backup(
            runtime_paths=source,
            repository_root=repository_root,
            backup_root=tmp_path / "backups",
            retention_count=3,
            authentication_key=TEST_AUTHENTICATION_KEY,
            git_commit=FIXED_COMMIT,
            created_at=FIXED_BACKUP_TIME,
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_restore_startup_block_01_stops_before_any_sqlite_open_when_intent_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app import main

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = initialized_runtime(tmp_path, repository_root, name="startup")
    _place_marker(paths.data_root)
    inspect_schema = Mock(side_effect=AssertionError("SQLite must stay closed"))
    initialize_schema = Mock(side_effect=AssertionError("SQLite must stay closed"))
    sqlite_connect = Mock(side_effect=AssertionError("SQLite must stay closed"))
    create_repository = Mock(side_effect=AssertionError("repository must not start"))
    create_wal_cleanup = Mock(side_effect=AssertionError("WAL cleanup must not start"))
    monkeypatch.setattr(main, "resolve_runtime_paths", lambda *_args: paths)
    monkeypatch.setattr(main, "inspect_conversation_history_schema", inspect_schema)
    monkeypatch.setattr(
        main, "initialize_conversation_history_schema", initialize_schema
    )
    monkeypatch.setattr(main.sqlite3, "connect", sqlite_connect)
    monkeypatch.setattr(main, "ConversationHistoryRepository", create_repository)
    monkeypatch.setattr(main, "ConversationWalCleanup", create_wal_cleanup)

    with pytest.raises(_recovery_error_type(), match=PUBLIC_RECOVERY_MESSAGE):
        async with main.lifespan(FastAPI()):
            pytest.fail("startup must not become ready while restore recovery is required")

    inspect_schema.assert_not_called()
    initialize_schema.assert_not_called()
    sqlite_connect.assert_not_called()
    create_repository.assert_not_called()
    create_wal_cleanup.assert_not_called()
    captured = capsys.readouterr()
    rendered = caplog.text + captured.out + captured.err
    assert "marker-secret-value" not in rendered
    assert "/private/conversations/miori.db" not in rendered


@pytest.mark.parametrize(
    "entry",
    (
        "create-backup",
        "restore-verify",
        "schema-inspect",
        "schema-initialize",
        "repository",
        "wal-cleanup",
        "sqlite-session",
    ),
)
def test_restore_startup_block_01_rejects_normal_sqlite_entry(
    tmp_path: Path,
    entry: str,
) -> None:
    from app.backup_restore import create_backup, verify_restored_backup
    from app.conversation_history._sqlite import SqliteSession
    from app.conversation_history.repository import ConversationHistoryRepository
    from app.conversation_history.schema import (
        initialize_conversation_history_schema,
        inspect_conversation_history_schema,
    )
    from app.conversation_history.wal_cleanup import ConversationWalCleanup

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    generation = _create_generation(tmp_path, repository_root)
    destination = initialized_runtime(tmp_path, repository_root, name="normal-entry")
    _place_marker(destination.data_root)
    connection_factory = Mock(side_effect=AssertionError("SQLite must stay closed"))
    wal_cleanup = Mock(spec=ConversationWalCleanup)

    def invoke_entry() -> None:
        if entry == "create-backup":
            create_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_root=tmp_path / "blocked-backups",
                retention_count=3,
                authentication_key=TEST_AUTHENTICATION_KEY,
                git_commit=FIXED_COMMIT,
                created_at=FIXED_BACKUP_TIME,
            )
            return
        if entry == "restore-verify":
            verify_restored_backup(
                runtime_paths=destination,
                repository_root=repository_root,
                backup_directory=generation,
                authentication_key=TEST_AUTHENTICATION_KEY,
            )
            return
        if entry == "schema-inspect":
            inspect_conversation_history_schema(destination.sqlite_path)
            return
        if entry == "schema-initialize":
            initialize_conversation_history_schema(destination.sqlite_path)
            return
        if entry == "repository":
            ConversationHistoryRepository(
                database_path=destination.sqlite_path,
                stale_after=timedelta(minutes=5),
                retention=timedelta(days=30),
                clock=lambda: datetime(2026, 8, 12, tzinfo=UTC),
                uuid_factory=lambda: UUID("3cd98d5e-74c6-45bf-8d62-dad3f5bb5759"),
                wal_cleanup=wal_cleanup,
                connection_factory=connection_factory,
            )
            return
        if entry == "wal-cleanup":
            ConversationWalCleanup(
                database_path=destination.sqlite_path,
                clock=lambda: datetime(2026, 8, 12, tzinfo=UTC),
                connection_factory=connection_factory,
            )
            return
        SqliteSession(destination.sqlite_path, connection_factory)

    with pytest.raises(_recovery_error_type(), match=PUBLIC_RECOVERY_MESSAGE):
        invoke_entry()

    connection_factory.assert_not_called()

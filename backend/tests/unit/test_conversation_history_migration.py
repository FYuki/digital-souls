import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.backup_restore.models import CONVERSATION_ARTIFACT_FILENAME
from app.conversation_history import schema
from app.conversation_history.errors import LegacySchemaError
from app.conversation_history.schema import (
    HISTORY_INDEX_SQL,
    STALE_INDEX_SQL,
    VERSION_THREE_CONVERSATION_TURNS_SQL,
    VERSION_TWO_CONVERSATIONS_SQL,
    initialize_conversation_history_schema,
)
from app.runtime_data_root import initialize_runtime_data_root
from app.runtime_paths import resolve_runtime_paths

CHARACTER_ID = "miori"
CONVERSATION_ID = "e98d6c65-1ae9-4d6f-a8c8-d59b0ad09001"
TURN_ID = "9e70795d-e5d5-431d-baa2-67f884403001"
CREATED_AT = "2026-07-24T00:00:00.000000Z"

def _create_version_two_database(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(VERSION_TWO_CONVERSATIONS_SQL)
        connection.execute(VERSION_THREE_CONVERSATION_TURNS_SQL)
        connection.execute(HISTORY_INDEX_SQL)
        connection.execute(STALE_INDEX_SQL)
        connection.execute(
            "INSERT INTO conversations "
            "(character_id, conversation_id, created_at) VALUES (?, ?, ?)",
            (CHARACTER_ID, CONVERSATION_ID, CREATED_AT),
        )
        connection.execute(
            "INSERT INTO conversation_turns "
            "(turn_id, character_id, conversation_id, user_content, "
            "assistant_content, status, privacy_reason_code, sanitizer_version, "
            "policy_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'completed', NULL, NULL, NULL, ?, ?)",
            (
                TURN_ID,
                CHARACTER_ID,
                CONVERSATION_ID,
                "マスク済みユーザー本文",
                "マスク済みアシスタント本文",
                CREATED_AT,
                CREATED_AT,
            ),
        )
        connection.execute("PRAGMA user_version = 2")


def _create_version_three_database(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(schema.CONVERSATIONS_SQL)
        connection.execute(VERSION_THREE_CONVERSATION_TURNS_SQL)
        connection.execute(schema.WAL_CLEANUP_JOBS_SQL)
        connection.execute(schema.HISTORY_INDEX_SQL)
        connection.execute(schema.STALE_INDEX_SQL)
        connection.execute(
            "INSERT INTO conversations "
            "(character_id, conversation_id, created_at) VALUES (?, ?, ?)",
            (CHARACTER_ID, CONVERSATION_ID, CREATED_AT),
        )
        connection.execute(
            "INSERT INTO conversation_turns "
            "(turn_id, character_id, conversation_id, user_content, "
            "assistant_content, status, privacy_reason_code, sanitizer_version, "
            "policy_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'completed', NULL, NULL, NULL, ?, ?)",
            (
                TURN_ID,
                CHARACTER_ID,
                CONVERSATION_ID,
                "マスク済みユーザー本文",
                "マスク済みアシスタント本文",
                CREATED_AT,
                CREATED_AT,
            ),
        )
        connection.execute("PRAGMA user_version = 3")


def _schema_state(database_path: Path) -> tuple[int, tuple[str, ...], set[str]]:
    with sqlite3.connect(database_path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        columns = tuple(
            str(row[1])
            for row in connection.execute("PRAGMA table_info(conversations)")
        )
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    return version, columns, tables


def test_should_migrate_canonical_version_two_database_to_version_four(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    _create_version_two_database(database_path)

    initialize_conversation_history_schema(database_path)

    version, conversation_columns, tables = _schema_state(database_path)

    assert version == 4
    assert conversation_columns == (
        "character_id",
        "conversation_id",
        "created_at",
        "archived_at",
    )
    assert tables == {"conversations", "conversation_turns", "wal_cleanup_jobs"}


def test_should_preserve_existing_rows_when_migrating_version_two_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    _create_version_two_database(database_path)

    initialize_conversation_history_schema(database_path)

    with sqlite3.connect(database_path) as connection:
        conversation = connection.execute(
            "SELECT character_id, conversation_id, created_at, archived_at "
            "FROM conversations"
        ).fetchone()
        turn = connection.execute(
            "SELECT turn_id, character_id, conversation_id, user_content, "
            "assistant_content, status FROM conversation_turns"
        ).fetchone()

    assert conversation == (CHARACTER_ID, CONVERSATION_ID, CREATED_AT, None)
    assert turn == (
        TURN_ID,
        CHARACTER_ID,
        CONVERSATION_ID,
        "マスク済みユーザー本文",
        "マスク済みアシスタント本文",
        "completed",
    )


def test_should_migrate_version_three_to_four_without_losing_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    _create_version_three_database(database_path)

    initialize_conversation_history_schema(database_path)

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        turn = connection.execute(
            "SELECT turn_id, user_content, assistant_content, status "
            "FROM conversation_turns"
        ).fetchone()

    assert version == 4
    assert turn == (
        TURN_ID,
        "マスク済みユーザー本文",
        "マスク済みアシスタント本文",
        "completed",
    )


def test_should_reject_version_three_migration_with_dependent_view(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    _create_version_three_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE VIEW completed_turns AS "
            "SELECT turn_id FROM conversation_turns WHERE status = 'completed'"
        )

    with pytest.raises(
        LegacySchemaError,
        match="conversation_turns migration does not support dependent views",
    ):
        initialize_conversation_history_schema(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT turn_id FROM completed_turns").fetchone() == (
            TURN_ID,
        )


@pytest.mark.parametrize(
    ("schema_sql", "object_type", "object_name"),
    [
        (
            "CREATE INDEX conversation_turns_custom_idx "
            "ON conversation_turns (updated_at)",
            "index",
            "conversation_turns_custom_idx",
        ),
        (
            "CREATE TRIGGER conversation_turns_audit "
            "AFTER UPDATE ON conversation_turns BEGIN SELECT NEW.turn_id; END",
            "trigger",
            "conversation_turns_audit",
        ),
    ],
)
def test_should_reject_version_three_migration_with_custom_schema_object(
    tmp_path: Path,
    schema_sql: str,
    object_type: str,
    object_name: str,
) -> None:
    database_path = tmp_path / "history.db"
    _create_version_three_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(schema_sql)

    with pytest.raises(
        LegacySchemaError,
        match="conversation_turns migration does not support custom indexes or triggers",
    ):
        initialize_conversation_history_schema(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND name = ?",
            (object_type, object_name),
        ).fetchone() == (object_name,)


def test_should_be_idempotent_after_migrating_version_two_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "history.db"
    _create_version_two_database(database_path)
    initialize_conversation_history_schema(database_path)

    initialize_conversation_history_schema(database_path)

    with sqlite3.connect(database_path) as connection:
        conversation_count = connection.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()[0]
        turn_count = connection.execute(
            "SELECT COUNT(*) FROM conversation_turns"
        ).fetchone()[0]
    assert conversation_count == 1
    assert turn_count == 1


def test_should_keep_version_two_database_unchanged_when_migration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "history.db"
    _create_version_two_database(database_path)
    original_connect = sqlite3.connect

    class FailingMigrationConnection(sqlite3.Connection):
        def execute(
            self,
            sql: str,
            parameters: tuple[object, ...] = (),
        ) -> sqlite3.Cursor:
            if sql == "PRAGMA user_version = 4":
                raise sqlite3.OperationalError("injected migration failure")
            return super().execute(sql, parameters)

    def failing_connect(path: Path) -> sqlite3.Connection:
        return original_connect(path, factory=FailingMigrationConnection)

    monkeypatch.setattr(schema.sqlite3, "connect", failing_connect)

    with pytest.raises(sqlite3.OperationalError, match="injected migration failure"):
        initialize_conversation_history_schema(database_path)

    with original_connect(database_path) as connection:
        conversation_count = connection.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()[0]
        turn_count = connection.execute(
            "SELECT COUNT(*) FROM conversation_turns"
        ).fetchone()[0]
    assert _schema_state(database_path) == (
        2,
        ("character_id", "conversation_id", "created_at"),
        {"conversations", "conversation_turns"},
    )
    assert conversation_count == 1
    assert turn_count == 1


def test_should_reject_noncanonical_version_two_database(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    _create_version_two_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX conversation_turns_history_idx")

    with pytest.raises(
        LegacySchemaError,
        match="existing database does not use current schema",
    ):
        initialize_conversation_history_schema(database_path)

    version, conversation_columns, tables = _schema_state(database_path)
    assert version == 2
    assert conversation_columns == (
        "character_id",
        "conversation_id",
        "created_at",
    )
    assert tables == {"conversations", "conversation_turns"}


def test_should_create_verified_backup_before_version_two_migration(
    tmp_path: Path,
) -> None:
    from app.backup_restore import create_backup, verify_backup
    from tests.backup_restore_test_support import TEST_AUTHENTICATION_KEY

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = resolve_runtime_paths(
        {
            "DS_ENVIRONMENT_ID": "dogfood",
            "DS_DATA_DIR": str(tmp_path / "runtime"),
        },
        repository_root,
    )
    initialize_runtime_data_root(paths, repository_root)
    _create_version_two_database(paths.sqlite_path)

    generation = create_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_root=tmp_path / "backups",
        retention_count=2,
        authentication_key=TEST_AUTHENTICATION_KEY,
        git_commit="0123456789abcdef0123456789abcdef01234567",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    result = verify_backup(
        backup_directory=generation,
        authentication_key=TEST_AUTHENTICATION_KEY,
    )
    conversation = result.artifact(CONVERSATION_ARTIFACT_FILENAME)

    assert conversation.schema_version == 2
    assert conversation.required_tables == {"conversations", "conversation_turns"}
    assert conversation.record_count == 1

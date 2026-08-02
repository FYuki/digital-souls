import sqlite3
from pathlib import Path

from app.conversation_history.schema import (
    initialize_conversation_history_schema,
)
from tests.conversation_history_test_support import create_repository


def test_should_create_nullable_archived_at_for_new_database(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"

    initialize_conversation_history_schema(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(conversations)")
        }
    assert "archived_at" in columns
    assert columns["archived_at"][3] == 0


def test_should_enable_secure_delete_on_repository_connections(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"
    initialize_conversation_history_schema(database_path)
    observed: list[int] = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=()):  # type: ignore[no-untyped-def]
            cursor = super().execute(sql, parameters)
            if sql.strip().upper() == "PRAGMA SECURE_DELETE = ON":
                observed.append(int(super().execute("PRAGMA secure_delete").fetchone()[0]))
            return cursor

    def connection_factory(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(path, factory=ObservedConnection)

    create_repository(
        database_path,
        connection_factory=connection_factory,
    ).create_conversation("miori")

    assert observed == [1]


def test_should_store_wal_cleanup_retry_as_metadata_only(tmp_path: Path) -> None:
    database_path = tmp_path / "history.db"

    initialize_conversation_history_schema(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(wal_cleanup_jobs)")
        }
    assert {
        "character_id",
        "conversation_id",
        "reason_code",
        "created_at",
        "attempt_count",
    } <= columns
    assert columns.isdisjoint(
        {"content", "body", "prompt", "detected_value", "raw_content_hash"}
    )

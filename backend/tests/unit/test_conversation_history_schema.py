import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from app.conversation_history.errors import LegacySchemaError
from app.conversation_history.schema import initialize_conversation_history_schema


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class TestConversationHistorySchema:
    def test_should_initialize_new_schema_from_empty_database(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "conversation-history.db"

        initialize_conversation_history_schema(database_path)

        with _connect(database_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]

        assert tables == {"conversations", "conversation_turns"}
        assert user_version > 0

    def test_should_be_idempotent_for_current_schema(self, tmp_path: Path) -> None:
        database_path = tmp_path / "conversation-history.db"
        initialize_conversation_history_schema(database_path)

        initialize_conversation_history_schema(database_path)

        with _connect(database_path) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name IN "
                "('conversations', 'conversation_turns')"
            ).fetchall()

        assert {row[0] for row in tables} == {
            "conversations",
            "conversation_turns",
        }

    def test_should_serialize_concurrent_initialization(self, tmp_path: Path) -> None:
        database_path = tmp_path / "conversation-history.db"
        worker_count = 8
        barrier = Barrier(worker_count)

        def initialize() -> None:
            barrier.wait()
            initialize_conversation_history_schema(database_path)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(initialize) for _ in range(worker_count)]

        for future in futures:
            future.result()

        with _connect(database_path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
            assert {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            } == {"conversations", "conversation_turns"}

    def test_should_reject_schema_with_matching_columns_but_missing_constraints(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "incomplete.db"
        with _connect(database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE conversations (
                    character_id TEXT,
                    conversation_id TEXT,
                    created_at TEXT
                );
                CREATE TABLE conversation_turns (
                    turn_id TEXT,
                    character_id TEXT,
                    conversation_id TEXT,
                    user_content TEXT,
                    assistant_content TEXT,
                    status TEXT,
                    privacy_reason_code TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                PRAGMA user_version = 1;
                """
            )

        with pytest.raises(LegacySchemaError):
            initialize_conversation_history_schema(database_path)

    def test_should_reject_current_tables_when_required_index_is_missing(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "incomplete.db"
        initialize_conversation_history_schema(database_path)
        with _connect(database_path) as connection:
            connection.execute("DROP INDEX conversation_turns_history_idx")

        with pytest.raises(LegacySchemaError):
            initialize_conversation_history_schema(database_path)

    def test_should_reject_legacy_schema_without_migrating_it(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "legacy.db"
        with _connect(database_path) as connection:
            connection.execute(
                "CREATE TABLE conversations ("
                "id INTEGER PRIMARY KEY, character TEXT, role TEXT, "
                "content TEXT, timestamp TEXT)"
            )

        with pytest.raises(LegacySchemaError):
            initialize_conversation_history_schema(database_path)

        with _connect(database_path) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(conversations)")
            }
            turn_table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'conversation_turns'"
            ).fetchone()

        assert columns == {"id", "character", "role", "content", "timestamp"}
        assert turn_table is None

    def test_should_enforce_character_conversation_parent_boundary(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "conversation-history.db"
        initialize_conversation_history_schema(database_path)

        with _connect(database_path) as connection:
            connection.execute(
                "INSERT INTO conversations "
                "(character_id, conversation_id, created_at) VALUES (?, ?, ?)",
                (
                    "miori",
                    "e98d6c65-1ae9-4d6f-a8c8-d59b0ad09001",
                    "2026-07-24T00:00:00.000000Z",
                ),
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO conversation_turns "
                    "(turn_id, character_id, conversation_id, user_content, "
                    "assistant_content, status, privacy_reason_code, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, NULL, 'processing', NULL, ?, ?)",
                    (
                        "9e70795d-e5d5-431d-baa2-67f884403001",
                        "akira",
                        "e98d6c65-1ae9-4d6f-a8c8-d59b0ad09001",
                        "マスク済み本文",
                        "2026-07-24T00:00:00.000000Z",
                        "2026-07-24T00:00:00.000000Z",
                    ),
                )

    @pytest.mark.parametrize(
        "conversation_id",
        [
            "g98d6c65-1ae9-4d6f-a8c8-d59b0ad09001",
            "e98d6c65-1ae9-4d6f-78c8-d59b0ad09001",
            "E98D6C65-1AE9-4D6F-A8C8-D59B0AD09001",
            "e98d6c651ae9-4d6f-a8c8-d59b0ad09001",
            "-98d6c65-1ae9-4d6f-a8c8-d59b0ad09001",
        ],
    )
    def test_should_reject_noncanonical_uuid4_conversation_id(
        self,
        tmp_path: Path,
        conversation_id: str,
    ) -> None:
        database_path = tmp_path / "conversation-history.db"
        initialize_conversation_history_schema(database_path)

        with _connect(database_path) as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO conversations "
                    "(character_id, conversation_id, created_at) VALUES (?, ?, ?)",
                    (
                        "miori",
                        conversation_id,
                        "2026-07-24T00:00:00.000000Z",
                    ),
                )

    @pytest.mark.parametrize(
        "turn_id",
        [
            "ge70795d-e5d5-431d-baa2-67f884403001",
            "9e70795d-e5d5-431d-7aa2-67f884403001",
            "9E70795D-E5D5-431D-BAA2-67F884403001",
            "9e70795de5d5-431d-baa2-67f884403001",
            "-e70795d-e5d5-431d-baa2-67f884403001",
        ],
    )
    def test_should_reject_noncanonical_uuid4_turn_id(
        self,
        tmp_path: Path,
        turn_id: str,
    ) -> None:
        database_path = tmp_path / "conversation-history.db"
        initialize_conversation_history_schema(database_path)

        with _connect(database_path) as connection:
            connection.execute(
                "INSERT INTO conversations "
                "(character_id, conversation_id, created_at) VALUES (?, ?, ?)",
                (
                    "miori",
                    "e98d6c65-1ae9-4d6f-a8c8-d59b0ad09001",
                    "2026-07-24T00:00:00.000000Z",
                ),
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO conversation_turns "
                    "(turn_id, character_id, conversation_id, user_content, "
                    "assistant_content, status, privacy_reason_code, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, NULL, 'processing', NULL, ?, ?)",
                    (
                        turn_id,
                        "miori",
                        "e98d6c65-1ae9-4d6f-a8c8-d59b0ad09001",
                        "マスク済み本文",
                        "2026-07-24T00:00:00.000000Z",
                        "2026-07-24T00:00:00.000000Z",
                    ),
                )

    @pytest.mark.parametrize(
        ("user_content", "assistant_content"),
        [
            ("保存禁止のuser原文", None),
            (None, "保存禁止のassistant原文"),
        ],
    )
    def test_should_enforce_privacy_skipped_body_constraint(
        self,
        tmp_path: Path,
        user_content: str | None,
        assistant_content: str | None,
    ) -> None:
        database_path = tmp_path / "conversation-history.db"
        initialize_conversation_history_schema(database_path)

        with _connect(database_path) as connection:
            connection.execute(
                "INSERT INTO conversations "
                "(character_id, conversation_id, created_at) VALUES (?, ?, ?)",
                (
                    "miori",
                    "e98d6c65-1ae9-4d6f-a8c8-d59b0ad09002",
                    "2026-07-24T00:00:00.000000Z",
                ),
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO conversation_turns "
                    "(turn_id, character_id, conversation_id, user_content, "
                    "assistant_content, status, privacy_reason_code, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'privacy_skipped', ?, ?, ?)",
                    (
                        "9e70795d-e5d5-431d-baa2-67f884403002",
                        "miori",
                        "e98d6c65-1ae9-4d6f-a8c8-d59b0ad09002",
                        user_content,
                        assistant_content,
                        "sensitive",
                        "2026-07-24T00:00:00.000000Z",
                        "2026-07-24T00:00:00.000000Z",
                    ),
                )

    def test_should_reject_unregistered_privacy_reason_in_direct_sql(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "conversation-history.db"
        initialize_conversation_history_schema(database_path)

        with _connect(database_path) as connection:
            connection.execute(
                "INSERT INTO conversations "
                "(character_id, conversation_id, created_at) VALUES (?, ?, ?)",
                (
                    "miori",
                    "e98d6c65-1ae9-4d6f-a8c8-d59b0ad09003",
                    "2026-07-24T00:00:00.000000Z",
                ),
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO conversation_turns "
                    "(turn_id, character_id, conversation_id, user_content, "
                    "assistant_content, status, privacy_reason_code, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, NULL, NULL, 'privacy_skipped', ?, ?, ?)",
                    (
                        "9e70795d-e5d5-431d-baa2-67f884403003",
                        "miori",
                        "e98d6c65-1ae9-4d6f-a8c8-d59b0ad09003",
                        "secret=raw-value",
                        "2026-07-24T00:00:00.000000Z",
                        "2026-07-24T00:00:00.000000Z",
                    ),
                )

    def test_should_create_indexes_for_history_and_stale_queries(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "conversation-history.db"

        initialize_conversation_history_schema(database_path)

        with _connect(database_path) as connection:
            indexes = {
                row[1]: tuple(
                    column[2]
                    for column in connection.execute(
                        f"PRAGMA index_info('{row[1]}')"
                    )
                )
                for row in connection.execute(
                    "PRAGMA index_list('conversation_turns')"
                )
                if row[3] == "c"
            }
            index_sql = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'index' AND tbl_name = 'conversation_turns'"
                )
            }

        assert (
            "character_id",
            "conversation_id",
            "created_at",
            "turn_id",
        ) in indexes.values()
        assert any(
            columns == ("updated_at",)
            and "where status = 'processing'" in index_sql[name].lower()
            for name, columns in indexes.items()
        )

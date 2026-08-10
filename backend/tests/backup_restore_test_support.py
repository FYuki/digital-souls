from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeAlias, TypeGuard

from app.backup_restore import BackupAuthenticationKey
from app.conversation_history.schema import initialize_conversation_history_schema
from app.conversation_history.schema import (
    CONVERSATION_TURNS_SQL,
    HISTORY_INDEX_SQL,
    STALE_INDEX_SQL,
)
from app.runtime_data_root import initialize_runtime_data_root
from app.runtime_paths import RuntimePaths, resolve_runtime_paths


FIXED_BACKUP_TIME = datetime(2026, 8, 8, 1, 2, 3, tzinfo=UTC)
FIXED_COMMIT = "0123456789abcdef0123456789abcdef01234567"
CONVERSATION_ID = "e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010"
TURN_ID = "9e70795d-e5d5-431d-baa2-67f884403010"
CONVERSATION_SENTINEL = "本文にだけ存在する秘密の会話"
SECRET_SENTINEL = "sk-test-backup-secret"
TEST_AUTHENTICATION_KEY = BackupAuthenticationKey(bytes.fromhex("ab" * 32))

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)

VERSION_TWO_CONVERSATIONS_SQL = """
CREATE TABLE conversations (
    character_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL CHECK (
        length(conversation_id) = 36
        AND length(replace(conversation_id, '-', '')) = 32
        AND substr(conversation_id, 9, 1) = '-'
        AND substr(conversation_id, 14, 1) = '-'
        AND substr(conversation_id, 15, 1) = '4'
        AND substr(conversation_id, 19, 1) = '-'
        AND substr(conversation_id, 20, 1) IN ('8', '9', 'a', 'b')
        AND substr(conversation_id, 24, 1) = '-'
        AND lower(conversation_id) = conversation_id
        AND replace(conversation_id, '-', '') NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY (character_id, conversation_id)
)
"""


def initialized_runtime(
    tmp_path: Path,
    repository_root: Path,
    environment_id: str = "test",
    name: str = "runtime",
) -> RuntimePaths:
    paths = resolve_runtime_paths(
        {
            "DS_ENVIRONMENT_ID": environment_id,
            "DS_DATA_DIR": str(tmp_path / name),
        },
        repository_root,
    )
    initialize_runtime_data_root(paths, repository_root)
    return paths


def create_history_database(paths: RuntimePaths, *, wal: bool) -> sqlite3.Connection:
    initialize_conversation_history_schema(paths.sqlite_path)
    connection = sqlite3.connect(paths.sqlite_path)
    if wal:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute(
        "INSERT INTO conversations (character_id, conversation_id, created_at) "
        "VALUES (?, ?, ?)",
        ("miori", CONVERSATION_ID, "2026-08-08T01:00:00.000000Z"),
    )
    connection.execute(
        "INSERT INTO conversation_turns "
        "(turn_id, character_id, conversation_id, user_content, "
        "assistant_content, status, privacy_reason_code, sanitizer_version, "
        "policy_version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'completed', NULL, NULL, NULL, ?, ?)",
        (
            TURN_ID,
            "miori",
            CONVERSATION_ID,
            CONVERSATION_SENTINEL,
            "保存済み回答",
            "2026-08-08T01:00:01.000000Z",
            "2026-08-08T01:00:02.000000Z",
        ),
    )
    connection.commit()
    return connection


def create_version_two_database(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(VERSION_TWO_CONVERSATIONS_SQL)
        connection.execute(CONVERSATION_TURNS_SQL)
        connection.execute(HISTORY_INDEX_SQL)
        connection.execute(STALE_INDEX_SQL)
        connection.execute(
            "INSERT INTO conversations (character_id, conversation_id, created_at) "
            "VALUES (?, ?, ?)",
            ("miori", CONVERSATION_ID, "2026-08-08T01:00:00.000000Z"),
        )
        connection.execute("PRAGMA user_version = 2")


def _is_json_object(value: object) -> TypeGuard[dict[str, JsonValue]]:
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(key, str) and _is_json_value(item) for key, item in value.items()
    )


def _is_json_value(value: object) -> TypeGuard[JsonValue]:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    return _is_json_object(value)


def read_json(path: Path) -> dict[str, JsonValue]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    assert _is_json_object(value)
    return value


def database_projection(database_path: Path) -> tuple[int, int, str]:
    with sqlite3.connect(database_path) as connection:
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        conversation_count = int(
            connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        )
        content = str(
            connection.execute(
                "SELECT user_content FROM conversation_turns WHERE turn_id = ?",
                (TURN_ID,),
            ).fetchone()[0]
        )
    return schema_version, conversation_count, content

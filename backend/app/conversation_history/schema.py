import sqlite3
from pathlib import Path

from app.conversation_history.errors import LegacySchemaError
from app.conversation_history.models import PrivacySkipReason

SCHEMA_VERSION = 1
CURRENT_TABLES = frozenset({"conversations", "conversation_turns"})
CONVERSATIONS_COLUMNS = (
    "character_id",
    "conversation_id",
    "created_at",
)
CONVERSATION_TURNS_COLUMNS = (
    "turn_id",
    "character_id",
    "conversation_id",
    "user_content",
    "assistant_content",
    "status",
    "privacy_reason_code",
    "created_at",
    "updated_at",
)
PRIVACY_SKIP_REASON_VALUES_SQL = ", ".join(
    f"'{reason.value}'" for reason in PrivacySkipReason
)


def _uuid4_check(column_name: str) -> str:
    return f"""
        length({column_name}) = 36
        AND length(replace({column_name}, '-', '')) = 32
        AND substr({column_name}, 9, 1) = '-'
        AND substr({column_name}, 14, 1) = '-'
        AND substr({column_name}, 15, 1) = '4'
        AND substr({column_name}, 19, 1) = '-'
        AND substr({column_name}, 20, 1) IN ('8', '9', 'a', 'b')
        AND substr({column_name}, 24, 1) = '-'
        AND lower({column_name}) = {column_name}
        AND replace({column_name}, '-', '') NOT GLOB '*[^0-9a-f]*'
    """.strip()


CONVERSATIONS_SQL = f"""
CREATE TABLE conversations (
    character_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL CHECK (
        {_uuid4_check("conversation_id")}
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY (character_id, conversation_id)
)
"""

CONVERSATION_TURNS_SQL = f"""
CREATE TABLE conversation_turns (
    turn_id TEXT PRIMARY KEY CHECK (
        {_uuid4_check("turn_id")}
    ),
    character_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_content TEXT,
    assistant_content TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('processing', 'completed', 'failed', 'privacy_skipped')
    ),
    privacy_reason_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (character_id, conversation_id)
        REFERENCES conversations (character_id, conversation_id),
    CHECK (
        (
            status = 'processing'
            AND user_content IS NOT NULL
            AND assistant_content IS NULL
            AND privacy_reason_code IS NULL
        )
        OR (
            status = 'completed'
            AND user_content IS NOT NULL
            AND assistant_content IS NOT NULL
            AND privacy_reason_code IS NULL
        )
        OR (
            status = 'failed'
            AND user_content IS NOT NULL
            AND privacy_reason_code IS NULL
        )
        OR (
            status = 'privacy_skipped'
            AND user_content IS NULL
            AND assistant_content IS NULL
            AND privacy_reason_code IN ({PRIVACY_SKIP_REASON_VALUES_SQL})
        )
    )
)
"""

HISTORY_INDEX_SQL = """
CREATE INDEX conversation_turns_history_idx
    ON conversation_turns (
        character_id,
        conversation_id,
        created_at,
        turn_id
    )
"""

STALE_INDEX_SQL = """
CREATE INDEX conversation_turns_stale_processing_idx
    ON conversation_turns (updated_at)
    WHERE status = 'processing'
"""

def _user_tables(connection: sqlite3.Connection) -> frozenset[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return frozenset(str(row[0]) for row in rows)


def _column_names(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[str, ...]:
    rows = connection.execute(f"PRAGMA table_info('{table_name}')")
    return tuple(str(row[1]) for row in rows)


def _normalized_sql(sql: str) -> str:
    return " ".join(sql.rstrip(";").split()).lower()


def _schema_object_sql(
    connection: sqlite3.Connection,
    object_type: str,
    name: str,
) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        (object_type, name),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return _normalized_sql(str(row[0]))


def _has_current_definitions(connection: sqlite3.Connection) -> bool:
    expected_definitions = (
        ("table", "conversations", CONVERSATIONS_SQL),
        ("table", "conversation_turns", CONVERSATION_TURNS_SQL),
        ("index", "conversation_turns_history_idx", HISTORY_INDEX_SQL),
        ("index", "conversation_turns_stale_processing_idx", STALE_INDEX_SQL),
    )
    return all(
        _schema_object_sql(connection, object_type, name) == _normalized_sql(sql)
        for object_type, name, sql in expected_definitions
    )


def _is_current_schema(connection: sqlite3.Connection) -> bool:
    return (
        _user_tables(connection) == CURRENT_TABLES
        and _column_names(connection, "conversations") == CONVERSATIONS_COLUMNS
        and _column_names(connection, "conversation_turns")
        == CONVERSATION_TURNS_COLUMNS
        and connection.execute("PRAGMA user_version").fetchone()[0]
        == SCHEMA_VERSION
        and _has_current_definitions(connection)
    )


def initialize_conversation_history_schema(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        tables = _user_tables(connection)
        if tables:
            if not _is_current_schema(connection):
                raise LegacySchemaError("existing database does not use current schema")
            connection.commit()
            return
        connection.execute(CONVERSATIONS_SQL)
        connection.execute(CONVERSATION_TURNS_SQL)
        connection.execute(HISTORY_INDEX_SQL)
        connection.execute(STALE_INDEX_SQL)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

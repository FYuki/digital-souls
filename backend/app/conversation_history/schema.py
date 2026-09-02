import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from app.conversation_history.errors import LegacySchemaError
from app.conversation_history.sqlite_lease import normal_sqlite_access
from app.conversation_history.titles import (
    CONVERSATION_TITLE_MAX_LENGTH,
    DEFAULT_CONVERSATION_TITLE,
    generate_conversation_title,
)
from app.privacy.contracts import HistoryDecisionReasonCode

SCHEMA_VERSION = 5
_VERSION_TWO_SCHEMA_VERSION = 2
_VERSION_THREE_SCHEMA_VERSION = 3
_VERSION_FOUR_SCHEMA_VERSION = 4
CURRENT_TABLES = frozenset(
    {
        "conversations",
        "conversation_turns",
        "wal_cleanup_jobs",
    }
)


@dataclass(frozen=True)
class SchemaInspection:
    schema_version: int
    tables: frozenset[str]
    is_current: bool
    migration_required: bool


def inspect_conversation_history_schema(database_path: Path) -> SchemaInspection:
    with normal_sqlite_access(database_path):
        return inspect_conversation_history_artifact_schema(database_path)


def inspect_conversation_history_artifact_schema(
    database_path: Path,
) -> SchemaInspection:
    if not database_path.is_file():
        return SchemaInspection(0, frozenset(), False, False)
    with closing(
        sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    ) as connection:
        tables = _user_tables(connection)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        return SchemaInspection(
            schema_version=version,
            tables=tables,
            is_current=_is_current_schema(connection),
            migration_required=(
                _is_version_two_schema(connection)
                or _is_version_three_schema(connection)
                or _is_version_four_schema(connection)
            ),
        )
_VERSION_TWO_TABLES = frozenset({"conversations", "conversation_turns"})
CONVERSATIONS_COLUMNS = (
    "character_id",
    "conversation_id",
    "created_at",
    "archived_at",
    "title",
    "title_is_manual",
)
VERSION_FOUR_CONVERSATIONS_COLUMNS = CONVERSATIONS_COLUMNS[:-2]
VERSION_TWO_CONVERSATIONS_COLUMNS = CONVERSATIONS_COLUMNS[:3]
CONVERSATION_TURNS_COLUMNS = (
    "turn_id",
    "character_id",
    "conversation_id",
    "user_content",
    "assistant_content",
    "status",
    "privacy_reason_code",
    "sanitizer_version",
    "policy_version",
    "created_at",
    "updated_at",
)
PRIVACY_SKIP_REASON_VALUES_SQL = ", ".join(
    f"'{reason.value}'" for reason in HistoryDecisionReasonCode
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


VERSION_FOUR_CONVERSATIONS_SQL = f"""
CREATE TABLE conversations (
    character_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL CHECK (
        {_uuid4_check("conversation_id")}
    ),
    created_at TEXT NOT NULL,
    archived_at TEXT,
    PRIMARY KEY (character_id, conversation_id)
)
"""

CONVERSATIONS_SQL = VERSION_FOUR_CONVERSATIONS_SQL.replace(
    "archived_at TEXT,",
    f"""archived_at TEXT,
    title TEXT NOT NULL DEFAULT '{DEFAULT_CONVERSATION_TITLE}' CHECK (
        length(title) BETWEEN 1 AND {CONVERSATION_TITLE_MAX_LENGTH}
        AND title = trim(title)
    ),
    title_is_manual INTEGER NOT NULL DEFAULT 0 CHECK (
        title_is_manual IN (0, 1)
    ),""",
)

VERSION_TWO_CONVERSATIONS_SQL = f"""
CREATE TABLE conversations (
    character_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL CHECK (
        {_uuid4_check("conversation_id")}
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY (character_id, conversation_id)
)
"""

_MIGRATED_CONVERSATIONS_SQL = VERSION_TWO_CONVERSATIONS_SQL.replace(
    "created_at TEXT NOT NULL,",
    "created_at TEXT NOT NULL, archived_at TEXT,",
)

_MIGRATED_TITLED_CONVERSATIONS_SQL = _MIGRATED_CONVERSATIONS_SQL.replace(
    "archived_at TEXT,",
    f"""archived_at TEXT,
    title TEXT NOT NULL DEFAULT '{DEFAULT_CONVERSATION_TITLE}' CHECK (
        length(title) BETWEEN 1 AND {CONVERSATION_TITLE_MAX_LENGTH}
        AND title = trim(title)
    ),
    title_is_manual INTEGER NOT NULL DEFAULT 0 CHECK (
        title_is_manual IN (0, 1)
    ),""",
)

WAL_CLEANUP_JOBS_SQL = f"""
CREATE TABLE wal_cleanup_jobs (
    character_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL CHECK (
        {_uuid4_check("conversation_id")}
    ),
    reason_code TEXT NOT NULL CHECK (reason_code = 'WAL_CHECKPOINT_FAILED'),
    created_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL CHECK (attempt_count > 0),
    PRIMARY KEY (character_id, conversation_id)
)
"""

VERSION_THREE_CONVERSATION_TURNS_SQL = f"""
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
    sanitizer_version TEXT,
    policy_version TEXT,
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
            AND sanitizer_version IS NULL
            AND policy_version IS NULL
        )
        OR (
            status = 'completed'
            AND user_content IS NOT NULL
            AND assistant_content IS NOT NULL
            AND privacy_reason_code IS NULL
            AND sanitizer_version IS NULL
            AND policy_version IS NULL
        )
        OR (
            status = 'failed'
            AND user_content IS NOT NULL
            AND privacy_reason_code IS NULL
            AND sanitizer_version IS NULL
            AND policy_version IS NULL
        )
        OR (
            status = 'privacy_skipped'
            AND user_content IS NULL
            AND assistant_content IS NULL
            AND privacy_reason_code IN ({PRIVACY_SKIP_REASON_VALUES_SQL})
            AND length(trim(sanitizer_version)) > 0
            AND length(trim(policy_version)) > 0
        )
    )
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
        status IN ('processing', 'completed', 'interrupted', 'failed', 'privacy_skipped')
    ),
    privacy_reason_code TEXT,
    sanitizer_version TEXT,
    policy_version TEXT,
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
            AND sanitizer_version IS NULL
            AND policy_version IS NULL
        )
        OR (
            status = 'completed'
            AND user_content IS NOT NULL
            AND assistant_content IS NOT NULL
            AND privacy_reason_code IS NULL
            AND sanitizer_version IS NULL
            AND policy_version IS NULL
        )
        OR (
            status = 'interrupted'
            AND user_content IS NOT NULL
            AND assistant_content IS NOT NULL
            AND privacy_reason_code IS NULL
            AND sanitizer_version IS NULL
            AND policy_version IS NULL
        )
        OR (
            status = 'failed'
            AND user_content IS NOT NULL
            AND privacy_reason_code IS NULL
            AND sanitizer_version IS NULL
            AND policy_version IS NULL
        )
        OR (
            status = 'privacy_skipped'
            AND user_content IS NULL
            AND assistant_content IS NULL
            AND privacy_reason_code IN ({PRIVACY_SKIP_REASON_VALUES_SQL})
            AND length(trim(sanitizer_version)) > 0
            AND length(trim(policy_version)) > 0
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
    normalized = " ".join(sql.rstrip(";").split()).lower()
    return (
        normalized.replace("( ", "(")
        .replace(" )", ")")
        .replace(" ,", ",")
    )


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
    conversations_definition = _schema_object_sql(
        connection,
        "table",
        "conversations",
    )
    expected_definitions = (
        ("table", "conversation_turns", CONVERSATION_TURNS_SQL),
        ("table", "wal_cleanup_jobs", WAL_CLEANUP_JOBS_SQL),
        ("index", "conversation_turns_history_idx", HISTORY_INDEX_SQL),
        ("index", "conversation_turns_stale_processing_idx", STALE_INDEX_SQL),
    )
    return conversations_definition in {
        _normalized_sql(CONVERSATIONS_SQL),
        _normalized_sql(_MIGRATED_TITLED_CONVERSATIONS_SQL),
    } and all(
        _schema_object_sql(connection, object_type, name) == _normalized_sql(sql)
        for object_type, name, sql in expected_definitions
    )


def _is_version_two_schema(connection: sqlite3.Connection) -> bool:
    expected_definitions = (
        ("table", "conversations", VERSION_TWO_CONVERSATIONS_SQL),
        ("table", "conversation_turns", VERSION_THREE_CONVERSATION_TURNS_SQL),
        ("index", "conversation_turns_history_idx", HISTORY_INDEX_SQL),
        ("index", "conversation_turns_stale_processing_idx", STALE_INDEX_SQL),
    )
    return (
        _user_tables(connection) == _VERSION_TWO_TABLES
        and _column_names(connection, "conversations")
        == VERSION_TWO_CONVERSATIONS_COLUMNS
        and _column_names(connection, "conversation_turns")
        == CONVERSATION_TURNS_COLUMNS
        and connection.execute("PRAGMA user_version").fetchone()[0]
        == _VERSION_TWO_SCHEMA_VERSION
        and all(
            _schema_object_sql(connection, object_type, name)
            == _normalized_sql(sql)
            for object_type, name, sql in expected_definitions
        )
    )


def _is_current_schema(connection: sqlite3.Connection) -> bool:
    return (
        _user_tables(connection) == CURRENT_TABLES
        and _has_current_schema_contract(connection)
    )


def _is_version_three_schema(connection: sqlite3.Connection) -> bool:
    expected_definitions = (
        ("table", "conversation_turns", VERSION_THREE_CONVERSATION_TURNS_SQL),
        ("table", "wal_cleanup_jobs", WAL_CLEANUP_JOBS_SQL),
        ("index", "conversation_turns_history_idx", HISTORY_INDEX_SQL),
        ("index", "conversation_turns_stale_processing_idx", STALE_INDEX_SQL),
    )
    return (
        _user_tables(connection) == CURRENT_TABLES
        and _schema_object_sql(connection, "table", "conversations")
        in {
            _normalized_sql(VERSION_FOUR_CONVERSATIONS_SQL),
            _normalized_sql(_MIGRATED_CONVERSATIONS_SQL),
        }
        and _column_names(connection, "conversations")
        == VERSION_FOUR_CONVERSATIONS_COLUMNS
        and _column_names(connection, "conversation_turns")
        == CONVERSATION_TURNS_COLUMNS
        and connection.execute("PRAGMA user_version").fetchone()[0]
        == _VERSION_THREE_SCHEMA_VERSION
        and all(
            _schema_object_sql(connection, object_type, name)
            == _normalized_sql(sql)
            for object_type, name, sql in expected_definitions
        )
    )


def _is_version_four_schema(connection: sqlite3.Connection) -> bool:
    expected_definitions = (
        ("table", "conversation_turns", CONVERSATION_TURNS_SQL),
        ("table", "wal_cleanup_jobs", WAL_CLEANUP_JOBS_SQL),
        ("index", "conversation_turns_history_idx", HISTORY_INDEX_SQL),
        ("index", "conversation_turns_stale_processing_idx", STALE_INDEX_SQL),
    )
    return (
        _user_tables(connection) == CURRENT_TABLES
        and _schema_object_sql(connection, "table", "conversations")
        in {
            _normalized_sql(VERSION_FOUR_CONVERSATIONS_SQL),
            _normalized_sql(_MIGRATED_CONVERSATIONS_SQL),
        }
        and _column_names(connection, "conversations")
        == VERSION_FOUR_CONVERSATIONS_COLUMNS
        and _column_names(connection, "conversation_turns")
        == CONVERSATION_TURNS_COLUMNS
        and connection.execute("PRAGMA user_version").fetchone()[0]
        == _VERSION_FOUR_SCHEMA_VERSION
        and all(
            _schema_object_sql(connection, object_type, name)
            == _normalized_sql(sql)
            for object_type, name, sql in expected_definitions
        )
    )


def _has_current_schema_contract(connection: sqlite3.Connection) -> bool:
    return (
        _column_names(connection, "conversations") == CONVERSATIONS_COLUMNS
        and _column_names(connection, "conversation_turns")
        == CONVERSATION_TURNS_COLUMNS
        and connection.execute("PRAGMA user_version").fetchone()[0]
        == SCHEMA_VERSION
        and _has_current_definitions(connection)
    )


def _migrate_version_two_schema(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE conversations ADD COLUMN archived_at TEXT")
    connection.execute(WAL_CLEANUP_JOBS_SQL)
    _migrate_turn_contract_to_version_four(connection)
    _migrate_conversation_titles_to_version_five(connection)
    if not _is_current_schema(connection):
        raise LegacySchemaError("version two migration did not create current schema")


def _migrate_turn_contract_to_version_four(connection: sqlite3.Connection) -> None:
    dependent_view = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'view' AND sql IS NOT NULL "
        "AND instr(lower(sql), lower(?)) > 0 LIMIT 1",
        ("conversation_turns",),
    ).fetchone()
    if dependent_view is not None:
        raise LegacySchemaError(
            "conversation_turns migration does not support dependent views"
        )
    unsupported_schema_object = connection.execute(
        "SELECT type, name FROM sqlite_master WHERE sql IS NOT NULL AND ("
        "(type = 'index' AND tbl_name = 'conversation_turns' "
        "AND name NOT IN (?, ?)) OR "
        "(type = 'trigger' AND (tbl_name = 'conversation_turns' "
        "OR instr(lower(sql), lower(?)) > 0))"
        ") LIMIT 1",
        (
            "conversation_turns_history_idx",
            "conversation_turns_stale_processing_idx",
            "conversation_turns",
        ),
    ).fetchone()
    if unsupported_schema_object is not None:
        raise LegacySchemaError(
            "conversation_turns migration does not support custom indexes or triggers"
        )
    connection.execute("DROP INDEX conversation_turns_history_idx")
    connection.execute("DROP INDEX conversation_turns_stale_processing_idx")
    connection.execute(
        "ALTER TABLE conversation_turns RENAME TO conversation_turns_version_three"
    )
    connection.execute(CONVERSATION_TURNS_SQL)
    columns = ", ".join(CONVERSATION_TURNS_COLUMNS)
    connection.execute(
        f"INSERT INTO conversation_turns ({columns}) "
        f"SELECT {columns} FROM conversation_turns_version_three"
    )
    connection.execute("DROP TABLE conversation_turns_version_three")
    connection.execute(HISTORY_INDEX_SQL)
    connection.execute(STALE_INDEX_SQL)
    connection.execute(f"PRAGMA user_version = {_VERSION_FOUR_SCHEMA_VERSION}")


def _migrate_version_three_schema(connection: sqlite3.Connection) -> None:
    _migrate_turn_contract_to_version_four(connection)
    _migrate_conversation_titles_to_version_five(connection)
    if not _is_current_schema(connection):
        raise LegacySchemaError("version three migration did not create current schema")


def _migrate_version_four_schema(connection: sqlite3.Connection) -> None:
    _migrate_conversation_titles_to_version_five(connection)
    if not _is_current_schema(connection):
        raise LegacySchemaError("version four migration did not create current schema")


def _migrate_conversation_titles_to_version_five(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        "ALTER TABLE conversations ADD COLUMN title TEXT NOT NULL "
        f"DEFAULT '{DEFAULT_CONVERSATION_TITLE}' CHECK ("
        f"length(title) BETWEEN 1 AND {CONVERSATION_TITLE_MAX_LENGTH} "
        "AND title = trim(title))"
    )
    connection.execute(
        "ALTER TABLE conversations ADD COLUMN title_is_manual INTEGER NOT NULL "
        "DEFAULT 0 CHECK (title_is_manual IN (0, 1))"
    )
    conversations = connection.execute(
        "SELECT character_id, conversation_id FROM conversations"
    ).fetchall()
    for character_id, conversation_id in conversations:
        first_content = connection.execute(
            "SELECT user_content FROM conversation_turns "
            "WHERE character_id = ? AND conversation_id = ? "
            "AND user_content IS NOT NULL "
            "ORDER BY created_at, rowid LIMIT 1",
            (character_id, conversation_id),
        ).fetchone()
        if first_content is None:
            continue
        connection.execute(
            "UPDATE conversations SET title = ? "
            "WHERE character_id = ? AND conversation_id = ?",
            (
                generate_conversation_title(str(first_content[0])),
                character_id,
                conversation_id,
            ),
        )
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def initialize_conversation_history_schema(database_path: Path) -> None:
    with normal_sqlite_access(database_path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("BEGIN IMMEDIATE")
            tables = _user_tables(connection)
            if tables:
                if _is_current_schema(connection):
                    connection.commit()
                    return
                if _is_version_two_schema(connection):
                    _migrate_version_two_schema(connection)
                    connection.commit()
                    return
                if _is_version_three_schema(connection):
                    _migrate_version_three_schema(connection)
                    connection.commit()
                    return
                if _is_version_four_schema(connection):
                    _migrate_version_four_schema(connection)
                    connection.commit()
                    return
                raise LegacySchemaError("existing database does not use current schema")
            connection.execute(CONVERSATIONS_SQL)
            connection.execute(CONVERSATION_TURNS_SQL)
            connection.execute(WAL_CLEANUP_JOBS_SQL)
            connection.execute(HISTORY_INDEX_SQL)
            connection.execute(STALE_INDEX_SQL)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

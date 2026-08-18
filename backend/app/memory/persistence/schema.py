from __future__ import annotations

import sqlite3
from pathlib import Path

from app.memory.persistence.sqlite import PersonaMemorySqlite
from app.runtime_data_root import initialize_runtime_data_root
from app.runtime_paths import RuntimePaths


PERSONA_MEMORY_TABLES = frozenset(
    {
        "approved_memories",
        "memory_sources",
        "memory_lineage",
        "memory_write_receipts",
        "memory_index_outbox",
        "temporary_provider_records",
    }
)
SCHEMA_VERSION = 1

APPROVED_MEMORIES_SQL = """
CREATE TABLE approved_memories (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL CHECK (length(trim(character_id)) > 0),
    provider_id TEXT NOT NULL CHECK (provider_id = 'core'),
    memory_kind TEXT NOT NULL CHECK (memory_kind IN ('EPISODIC', 'SEMANTIC')),
    memory_type TEXT NOT NULL CHECK (
        memory_type IN (
            'EPISODIC_EVENT',
            'USER_PREFERENCE',
            'INTERACTION_PREFERENCE'
        )
    ),
    episodic_event_type TEXT CHECK (
        episodic_event_type IS NULL OR episodic_event_type IN (
            'SHARED_MILESTONE', 'ACHIEVEMENT', 'DECISION', 'OUTCOME', 'CHANGE'
        )
    ),
    formation_method TEXT NOT NULL CHECK (
        formation_method IN ('DIRECT', 'EXTRACTED', 'ADDON_EVENT', 'CONSOLIDATED')
    ),
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    normalized_text TEXT NOT NULL CHECK (length(trim(normalized_text)) > 0),
    structured_value TEXT NOT NULL CHECK (length(trim(structured_value)) > 0),
    policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0),
    classifier_version TEXT NOT NULL CHECK (length(trim(classifier_version)) > 0),
    model_id TEXT NOT NULL CHECK (length(trim(model_id)) > 0),
    model_digest TEXT NOT NULL CHECK (length(trim(model_digest)) > 0),
    prompt_version TEXT NOT NULL CHECK (length(trim(prompt_version)) > 0),
    content_version INTEGER NOT NULL CHECK (content_version > 0),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'INACTIVE')),
    idempotency_key TEXT NOT NULL CHECK (length(trim(idempotency_key)) > 0),
    last_write_idempotency_key TEXT CHECK (
        last_write_idempotency_key IS NULL
        OR length(trim(last_write_idempotency_key)) > 0
    ),
    effective_at TEXT NOT NULL,
    effective_timezone TEXT NOT NULL CHECK (length(trim(effective_timezone)) > 0),
    temporal_precision TEXT NOT NULL CHECK (
        temporal_precision IN ('YEAR', 'MONTH', 'DAY', 'HOUR', 'MINUTE', 'SECOND')
    ),
    expires_at TEXT,
    last_user_mentioned_at TEXT,
    last_consolidated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (character_id, id),
    UNIQUE (character_id, idempotency_key),
    CHECK (
        (memory_type = 'EPISODIC_EVENT' AND memory_kind = 'EPISODIC'
            AND episodic_event_type IS NOT NULL)
        OR
        (memory_type IN ('USER_PREFERENCE', 'INTERACTION_PREFERENCE')
            AND memory_kind = 'SEMANTIC' AND episodic_event_type IS NULL)
    )
)
"""

MEMORY_SOURCES_SQL = """
CREATE TABLE memory_sources (
    character_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (
        source_type IN ('CONVERSATION_TURN', 'PROVIDER_RECORD', 'ADDON_EVENT')
    ),
    source_provider_id TEXT NOT NULL CHECK (length(trim(source_provider_id)) > 0),
    source_ref TEXT NOT NULL CHECK (length(trim(source_ref)) > 0),
    PRIMARY KEY (
        character_id, memory_id, source_type, source_provider_id, source_ref
    ),
    FOREIGN KEY (character_id, memory_id)
        REFERENCES approved_memories (character_id, id) ON DELETE CASCADE
)
"""

MEMORY_LINEAGE_SQL = """
CREATE TABLE memory_lineage (
    character_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    related_memory_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (
        relation IN ('CONSOLIDATED_FROM', 'SUPERSEDES', 'DUPLICATE_OF')
    ),
    PRIMARY KEY (character_id, memory_id, related_memory_id, relation),
    FOREIGN KEY (character_id, memory_id)
        REFERENCES approved_memories (character_id, id) ON DELETE CASCADE,
    FOREIGN KEY (character_id, related_memory_id)
        REFERENCES approved_memories (character_id, id) ON DELETE CASCADE,
    CHECK (memory_id <> related_memory_id)
)
"""

MEMORY_WRITE_RECEIPTS_SQL = """
CREATE TABLE memory_write_receipts (
    character_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK (length(trim(idempotency_key)) > 0),
    memory_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('SAVE', 'CORRECT')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (character_id, idempotency_key),
    FOREIGN KEY (character_id, memory_id)
        REFERENCES approved_memories (character_id, id) ON DELETE CASCADE
)
"""

MEMORY_INDEX_OUTBOX_SQL = """
CREATE TABLE memory_index_outbox (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    character_id TEXT NOT NULL CHECK (length(trim(character_id)) > 0),
    operation TEXT NOT NULL CHECK (operation IN ('UPSERT', 'DELETE')),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'COMPLETED', 'FAILED')),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

TEMPORARY_PROVIDER_RECORDS_SQL = """
CREATE TABLE temporary_provider_records (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL CHECK (length(trim(character_id)) > 0),
    provider_id TEXT NOT NULL CHECK (
        provider_id IN ('temporary:agriculture', 'temporary:recipe')
    ),
    source_ref TEXT NOT NULL CHECK (length(trim(source_ref)) > 0),
    record_type TEXT NOT NULL CHECK (length(trim(record_type)) > 0),
    structured_value TEXT NOT NULL CHECK (length(trim(structured_value)) > 0),
    effective_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (character_id, provider_id, source_ref)
)
"""

INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_memory_index_outbox_pending "
    "ON memory_index_outbox (status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_approved_memories_active "
    "ON approved_memories (character_id, status, created_at, id)",
)


def initialize_persona_memory_schema(
    paths: RuntimePaths,
    repository_root: Path,
) -> None:
    initialize_runtime_data_root(paths, repository_root)
    database = PersonaMemorySqlite(paths.persona_memory_sqlite_path, sqlite3.connect)
    with database.transaction() as connection:
        tables = _user_tables(connection)
        if tables and tables != PERSONA_MEMORY_TABLES:
            raise ValueError("existing persona memory database has an unknown schema")
        if tables:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise ValueError("existing persona memory database has an unknown schema")
        else:
            connection.execute(APPROVED_MEMORIES_SQL)
            connection.execute(MEMORY_SOURCES_SQL)
            connection.execute(MEMORY_LINEAGE_SQL)
            connection.execute(MEMORY_WRITE_RECEIPTS_SQL)
            connection.execute(MEMORY_INDEX_OUTBOX_SQL)
            connection.execute(TEMPORARY_PROVIDER_RECORDS_SQL)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        for statement in INDEX_SQL:
            connection.execute(statement)
    database.truncate_wal()


def _user_tables(connection: sqlite3.Connection) -> frozenset[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return frozenset(str(row[0]) for row in rows)

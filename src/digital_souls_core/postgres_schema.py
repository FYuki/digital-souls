"""Current PostgreSQL schema and atomic source revocation."""

from typing import Any
from uuid import uuid4

from psycopg import Connection

from .postgres_record_revocation import revoke_records
from .postgres_record_schema import COLUMNS, RECORD_DDL
from .postgres_record_schema import CONSTRAINTS as RECORD_CONSTRAINTS

SCHEMA_VERSION = 7

# Tables present through version 5, used only to validate historical migration sources.
LEGACY_VERBATIM_INDEXES = {("memory_source_lookup", "i")}
LEGACY_VERBATIM_COLUMNS = {
    "memory_jobs": ("seq", "id", "binding", "sources", "versions", "state"),
    "memories": ("seq", "id", "binding", "job", "kind", "body", "state", "revoked_by"),
    "memory_sources": ("seq", "binding", "memory", "conversation", "revision", "position", "epoch"),
}

TABLE_COLUMNS = {
    "schema_version": ("version",),
    "conversations": (
        "seq",
        "binding",
        "id",
        "revision",
        "private_mode",
        "archived",
        "memory_epoch",
    ),
    "turns": (
        "seq",
        "binding",
        "conversation",
        "request",
        "fingerprint",
        "revision",
        "messages",
        "finish",
        "memory_excluded",
        "private_mode",
        "stated_at",
        "memory_confirmation",
        "card_sha256",
    ),
    "source_deletions": ("seq", "event", "binding", "conversation", "through_revision"),
    "memory_events": ("seq", "id", "binding", "conversation", "epoch", "reason", "processed"),
    "turn_tombstones": ("seq", "binding", "conversation", "request", "revision"),
    "turn_deletions": ("seq", "event", "binding", "conversation", "turn_revisions"),
}

# These are the keys and referential actions on which receipt and deletion guarantees rely.
LEGACY_VERBATIM_CONSTRAINTS = {
    ("memory_jobs", "memory_jobs_pkey"): "PRIMARY KEY (binding, id)",
    ("memories", "memories_pkey"): "PRIMARY KEY (binding, id)",
    (
        "memories",
        "memories_binding_job_fkey",
    ): "FOREIGN KEY (binding, job) REFERENCES memory_jobs(binding, id)",
    (
        "memory_sources",
        "memory_sources_pkey",
    ): 'PRIMARY KEY (binding, memory, conversation, revision, "position")',
    (
        "memory_sources",
        "memory_sources_binding_memory_fkey",
    ): "FOREIGN KEY (binding, memory) REFERENCES memories(binding, id) ON DELETE CASCADE",
    ("memory_jobs", "memory_jobs_seq_key"): "UNIQUE (seq)",
    ("memories", "memories_seq_key"): "UNIQUE (seq)",
    ("memory_sources", "memory_sources_seq_key"): "UNIQUE (seq)",
}

CONSTRAINTS = {
    ("schema_version", "schema_version_pkey"): "PRIMARY KEY (version)",
    ("conversations", "conversations_pkey"): "PRIMARY KEY (binding, id)",
    ("turns", "turns_pkey"): "PRIMARY KEY (binding, conversation, request)",
    (
        "turns",
        "turns_binding_conversation_revision_key",
    ): "UNIQUE (binding, conversation, revision)",
    (
        "turns",
        "turns_binding_conversation_fkey",
    ): (
        "FOREIGN KEY (binding, conversation) REFERENCES conversations(binding, id) "
        "ON DELETE CASCADE"
    ),
    ("source_deletions", "source_deletions_pkey"): "PRIMARY KEY (event)",
    ("memory_events", "memory_events_pkey"): "PRIMARY KEY (id)",
    ("turn_tombstones", "turn_tombstones_pkey"): "PRIMARY KEY (binding, conversation, request)",
    (
        "turn_tombstones",
        "turn_tombstones_binding_conversation_fkey",
    ): (
        "FOREIGN KEY (binding, conversation) REFERENCES conversations(binding, id) "
        "ON DELETE CASCADE"
    ),
    ("turn_deletions", "turn_deletions_pkey"): "PRIMARY KEY (event)",
}
CONSTRAINTS.update(
    {
        (table, table + "_seq_key"): "UNIQUE (seq)"
        for table in TABLE_COLUMNS
        if table != "schema_version"
    }
)

TABLE_COLUMNS.update({table: tuple(columns) for table, columns in COLUMNS.items()})
CONSTRAINTS.update(RECORD_CONSTRAINTS)

TURN_DELETION_DDL = (
    """CREATE TABLE turn_tombstones (
        seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
        binding TEXT NOT NULL, conversation TEXT NOT NULL, request TEXT NOT NULL,
        revision INTEGER NOT NULL, PRIMARY KEY(binding,conversation,request),
        FOREIGN KEY(binding,conversation) REFERENCES conversations(binding,id) ON DELETE CASCADE
    )""",
    """CREATE TABLE turn_deletions (
        seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
        event TEXT PRIMARY KEY, binding TEXT NOT NULL, conversation TEXT NOT NULL,
        turn_revisions TEXT NOT NULL
    )""",
)

DDL = (
    "CREATE TABLE schema_version (version INTEGER PRIMARY KEY)",
    """CREATE TABLE conversations (
        seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
        binding TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
        private_mode BOOLEAN NOT NULL DEFAULT false, archived BOOLEAN NOT NULL DEFAULT false,
        memory_epoch INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(binding,id)
    )""",
    """CREATE TABLE turns (
        seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
        binding TEXT NOT NULL, conversation TEXT NOT NULL, request TEXT NOT NULL,
        fingerprint TEXT NOT NULL, revision INTEGER NOT NULL, messages TEXT NOT NULL,
        finish TEXT NOT NULL, memory_excluded TEXT NOT NULL DEFAULT '[]',
        private_mode BOOLEAN NOT NULL DEFAULT false, stated_at TIMESTAMPTZ,
        memory_confirmation TEXT NOT NULL DEFAULT '{}', card_sha256 TEXT,
        PRIMARY KEY(binding,conversation,request), UNIQUE(binding,conversation,revision),
        FOREIGN KEY(binding,conversation) REFERENCES conversations(binding,id) ON DELETE CASCADE
    )""",
    """CREATE TABLE source_deletions (
        seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
        event TEXT PRIMARY KEY, binding TEXT NOT NULL, conversation TEXT NOT NULL,
        through_revision INTEGER NOT NULL
    )""",
    """CREATE TABLE memory_events (
        seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
        id TEXT PRIMARY KEY, binding TEXT NOT NULL, conversation TEXT NOT NULL,
        epoch INTEGER NOT NULL, reason TEXT NOT NULL, processed BOOLEAN NOT NULL DEFAULT false
    )""",
    *TURN_DELETION_DDL,
    *RECORD_DDL,
    f"INSERT INTO schema_version(version) VALUES ({SCHEMA_VERSION})",
)


def create(db: Connection[tuple[Any, ...]]) -> None:
    for statement in DDL:
        db.execute(statement)


def revoke(
    db: Connection[tuple[Any, ...]],
    binding: str,
    conversation: str,
    epoch: int,
    reason: str,
) -> None:
    event = str(uuid4())
    db.execute(
        "INSERT INTO memory_events(id,binding,conversation,epoch,reason) VALUES (%s,%s,%s,%s,%s)",
        (event, binding, conversation, epoch, reason),
    )
    revoke_records(db, binding, conversation, event)


def revoke_turns(
    db: Connection[tuple[Any, ...]],
    binding: str,
    conversation: str,
    epoch: int,
    revisions: tuple[int, ...],
) -> None:
    event = str(uuid4())
    db.execute(
        "INSERT INTO memory_events(id,binding,conversation,epoch,reason) VALUES (%s,%s,%s,%s,%s)",
        (event, binding, conversation, epoch, "turn_delete"),
    )
    revoke_records(db, binding, conversation, event, revisions)

"""会話の変更と同時に確定する抽出予約。本文は持たない。"""

import sqlite3

MEMORY_QUEUE_TABLES = frozenset(
    {"memory_thread_jobs", "memory_turn_versions", "memory_thread_receipts"}
)

JOB_SQL = """CREATE TABLE memory_thread_jobs (
    character_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    completed_revision INTEGER NOT NULL DEFAULT 0 CHECK(completed_revision >= 0),
    lease_token TEXT,
    leased_revision INTEGER,
    lease_until REAL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    last_outcome TEXT NOT NULL DEFAULT 'PENDING' CHECK(last_outcome IN ('PENDING','SAVED','REJECTED','EMPTY','MIXED','FAILED','STALE')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(character_id, conversation_id),
    CHECK((lease_token IS NULL AND leased_revision IS NULL AND lease_until IS NULL)
        OR (lease_token IS NOT NULL AND leased_revision > 0 AND lease_until IS NOT NULL)),
    CHECK(completed_revision <= revision)
)"""
VERSION_SQL = """CREATE TABLE memory_turn_versions (
    character_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
    PRIMARY KEY(character_id, conversation_id, turn_id)
)"""
RECEIPT_SQL = """CREATE TABLE memory_thread_receipts (
    character_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('SAVED','REJECTED','EMPTY','MIXED')),
    saved_count INTEGER NOT NULL CHECK(saved_count >= 0),
    rejected_count INTEGER NOT NULL CHECK(rejected_count >= 0),
    completed_at TEXT NOT NULL,
    PRIMARY KEY(character_id, conversation_id, revision)
)"""


def _reserve(character: str, conversation: str) -> str:
    return f"""INSERT INTO memory_thread_jobs(character_id, conversation_id, revision, updated_at)
    VALUES({character}, {conversation}, 1, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    ON CONFLICT(character_id, conversation_id) DO UPDATE SET revision = revision + 1,
    next_attempt_at = 0, updated_at = excluded.updated_at;"""


def _turn_trigger(operation: str) -> tuple[str, str, str]:
    name = "memory_turn_" + operation.lower()
    ref = "OLD" if operation == "DELETE" else "NEW"
    deleted = 1 if operation == "DELETE" else 0
    when = ""
    if operation == "UPDATE":
        fields = (
            "user_content",
            "assistant_content",
            "status",
            "privacy_reason_code",
            "sanitizer_version",
            "policy_version",
            "created_at",
            "updated_at",
        )
        when = " WHEN " + " OR ".join(
            f"OLD.{field} IS NOT NEW.{field}" for field in fields
        )
    sql = f"""CREATE TRIGGER {name} AFTER {operation} ON conversation_turns{when} BEGIN
        INSERT INTO memory_turn_versions(character_id, conversation_id, turn_id, revision, deleted)
        VALUES({ref}.character_id, {ref}.conversation_id, {ref}.turn_id, 1, {deleted})
        ON CONFLICT(character_id, conversation_id, turn_id) DO UPDATE SET revision = revision + 1, deleted = {deleted};
        {_reserve(ref + ".character_id", ref + ".conversation_id")}
    END"""
    return "trigger", name, sql


def _screen_trigger(operation: str) -> tuple[str, str, str]:
    name = "memory_screen_" + operation.lower()
    refs = ("OLD", "NEW") if operation == "UPDATE" else (
        ("OLD",) if operation == "DELETE" else ("NEW",)
    )
    ids = ", ".join(ref + ".turn_id" for ref in refs)
    sql = f"""CREATE TRIGGER {name} AFTER {operation} ON screen_turn_provenance BEGIN
        UPDATE memory_turn_versions SET revision = revision + 1 WHERE turn_id IN ({ids});
        INSERT INTO memory_thread_jobs(character_id, conversation_id, revision, updated_at)
        SELECT character_id, conversation_id, 1, strftime('%Y-%m-%dT%H:%M:%fZ','now')
        FROM conversation_turns WHERE turn_id IN ({ids})
        ON CONFLICT(character_id, conversation_id) DO UPDATE SET revision = revision + 1,
        next_attempt_at = 0, updated_at = excluded.updated_at;
    END"""
    return "trigger", name, sql


MEMORY_QUEUE_DEFINITIONS = (
    ("table", "memory_thread_jobs", JOB_SQL),
    ("table", "memory_turn_versions", VERSION_SQL),
    ("table", "memory_thread_receipts", RECEIPT_SQL),
    ("trigger", "memory_turn_identity", """CREATE TRIGGER memory_turn_identity
        BEFORE UPDATE OF turn_id, character_id, conversation_id ON conversation_turns
        WHEN OLD.turn_id != NEW.turn_id OR OLD.character_id != NEW.character_id
          OR OLD.conversation_id != NEW.conversation_id
        BEGIN SELECT RAISE(ABORT,'conversation source identity is immutable'); END"""),
    *(_turn_trigger(operation) for operation in ("INSERT", "UPDATE", "DELETE")),
    *(_screen_trigger(operation) for operation in ("INSERT", "UPDATE", "DELETE")),
)


def add_memory_queue_schema(connection: sqlite3.Connection) -> None:
    for _, _, sql in MEMORY_QUEUE_DEFINITIONS:
        connection.execute(sql)
    # 会話履歴を変更せず、既存スレッドの抽出予約と初期版だけを作る。
    connection.execute(
        "INSERT INTO memory_turn_versions(character_id, conversation_id, turn_id, revision, deleted) SELECT character_id, conversation_id, turn_id, 1, 0 FROM conversation_turns"
    )
    connection.execute(
        "INSERT INTO memory_thread_jobs(character_id, conversation_id, revision, updated_at) SELECT character_id, conversation_id, 1, MAX(updated_at) FROM conversation_turns GROUP BY character_id, conversation_id"
    )

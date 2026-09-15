"""既存persona SQLiteへ意味記憶の独立正本を加える。"""

import sqlite3

TABLES = frozenset({
    "semantic_records", "semantic_versions", "semantic_relations", "semantic_receipts",
    "semantic_processed_sources", "semantic_deletion_masks", "semantic_reassessment",
})

DEFINITIONS = (
    """CREATE TABLE semantic_records (
        id TEXT PRIMARY KEY, character_id TEXT NOT NULL CHECK(length(trim(character_id))>0),
        formation_type TEXT NOT NULL CHECK(formation_type IN ('DIRECT_EXTRACTION','EXPERIENCE_DERIVED')),
        content_version INTEGER NOT NULL CHECK(content_version>0),
        status TEXT NOT NULL CHECK(status IN ('ACTIVE','HISTORICAL','SUPERSEDED','CONFLICTED','INACTIVE','DELETED')),
        proposition TEXT CHECK(proposition IS NULL OR json_valid(proposition)),
        sources TEXT NOT NULL CHECK(json_valid(sources) AND json_array_length(sources)>0),
        confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1),
        stamp TEXT NOT NULL CHECK(json_valid(stamp)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(character_id,id),
        CHECK((status='DELETED' AND proposition IS NULL) OR (status!='DELETED' AND proposition IS NOT NULL))
    )""",
    """CREATE TABLE semantic_versions (
        character_id TEXT NOT NULL, record_id TEXT NOT NULL,
        content_version INTEGER NOT NULL CHECK(content_version>0),
        proposition TEXT CHECK(proposition IS NULL OR json_valid(proposition)),
        sources TEXT NOT NULL CHECK(json_valid(sources)), stamp TEXT NOT NULL CHECK(json_valid(stamp)),
        created_at TEXT NOT NULL,
        PRIMARY KEY(character_id,record_id,content_version),
        FOREIGN KEY(character_id,record_id) REFERENCES semantic_records(character_id,id)
    )""",
    """CREATE TABLE semantic_relations (
        character_id TEXT NOT NULL, source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
        target_id TEXT NOT NULL, target_version INTEGER NOT NULL,
        relation TEXT NOT NULL CHECK(relation IN ('REAFFIRM','CORRECT','CHANGE','CONFLICT','SELF_REPORT')),
        created_at TEXT NOT NULL, CHECK(source_id!=target_id),
        PRIMARY KEY(character_id,source_id,source_version,target_id,target_version,relation),
        FOREIGN KEY(character_id,source_id,source_version)
          REFERENCES semantic_versions(character_id,record_id,content_version),
        FOREIGN KEY(character_id,target_id,target_version)
          REFERENCES semantic_versions(character_id,record_id,content_version)
    )""",
    """CREATE TABLE semantic_receipts (
        character_id TEXT NOT NULL, receipt_key TEXT NOT NULL,
        record_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(character_id,receipt_key),
        FOREIGN KEY(character_id,record_id) REFERENCES semantic_records(character_id,id)
    )""",
    """CREATE TABLE semantic_processed_sources (
        character_id TEXT NOT NULL, conversation_id TEXT NOT NULL, source_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0), processed_at TEXT NOT NULL,
        PRIMARY KEY(character_id,conversation_id,source_id,revision)
    )""",
    """CREATE TABLE semantic_deletion_masks (
        character_id TEXT NOT NULL, record_id TEXT NOT NULL,
        source TEXT NOT NULL CHECK(json_valid(source)),
        PRIMARY KEY(character_id,record_id,source),
        FOREIGN KEY(character_id,record_id) REFERENCES semantic_records(character_id,id)
    )""",
    """CREATE TABLE semantic_reassessment (
        character_id TEXT NOT NULL, record_id TEXT NOT NULL, content_version INTEGER NOT NULL,
        formation_type TEXT NOT NULL, reason TEXT NOT NULL,
        created_at TEXT NOT NULL, completed_at TEXT,
        PRIMARY KEY(character_id,record_id,content_version),
        FOREIGN KEY(character_id,record_id) REFERENCES semantic_records(character_id,id)
    )""",
    """CREATE INDEX idx_semantic_character ON semantic_records(character_id,status,updated_at)""",
    """CREATE TRIGGER semantic_identity BEFORE UPDATE ON semantic_records
       WHEN NEW.id!=OLD.id OR NEW.character_id!=OLD.character_id OR NEW.formation_type!=OLD.formation_type
       BEGIN SELECT RAISE(ABORT,'semantic identity is immutable'); END""",
    """CREATE TRIGGER semantic_no_revival BEFORE UPDATE ON semantic_records
       WHEN OLD.status='DELETED' AND (NEW.status!='DELETED' OR NEW.proposition IS NOT NULL)
       BEGIN SELECT RAISE(ABORT,'deleted semantic cannot be revived'); END""",
    """CREATE TRIGGER semantic_version_immutable BEFORE UPDATE ON semantic_versions
       WHEN NEW.character_id!=OLD.character_id OR NEW.record_id!=OLD.record_id
         OR NEW.content_version!=OLD.content_version OR NEW.sources!=OLD.sources
         OR NEW.stamp!=OLD.stamp OR NEW.created_at!=OLD.created_at
         OR (NEW.proposition IS NOT OLD.proposition AND NEW.proposition IS NOT NULL)
       BEGIN SELECT RAISE(ABORT,'semantic version is immutable'); END""",
    """CREATE TRIGGER semantic_version_no_delete BEFORE DELETE ON semantic_versions
       BEGIN SELECT RAISE(ABORT,'semantic version cannot be deleted'); END""",
)


def initialize_schema(connection: sqlite3.Connection) -> None:
    for definition in DEFINITIONS:
        connection.execute(definition)


def validate_schema(connection: sqlite3.Connection) -> None:
    expected = sqlite3.connect(":memory:")
    try:
        initialize_schema(expected)
        for kind, name, sql in expected.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL"
        ):
            actual = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name),
            ).fetchone()
            if actual is None or " ".join(actual[0].split()) != " ".join(sql.split()):
                raise ValueError("existing semantic database has an unknown schema")
    finally:
        expected.close()

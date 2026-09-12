"""Episode / Factは同一SQLite内の独立レコードと版付き参照で保存する。"""

import sqlite3

EPISODIC_TABLES = frozenset({
    "episodic_records", "episodic_versions", "episodic_links",
    "episodic_merges", "episodic_receipts", "episodic_invalid_sources",
})

DEFINITIONS = (
    """CREATE TABLE episodic_invalid_sources (
        character_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision > 0),
        invalidated_at TEXT NOT NULL,
        PRIMARY KEY(character_id,source_id,revision)
    )""",
    """CREATE TABLE episodic_records (
        id TEXT PRIMARY KEY,
        character_id TEXT NOT NULL CHECK(length(trim(character_id)) > 0),
        kind TEXT NOT NULL CHECK(kind IN ('EPISODE','FACT')),
        conversation_id TEXT,
        content_version INTEGER NOT NULL CHECK(content_version > 0),
        status TEXT NOT NULL CHECK(status IN ('ACTIVE','INACTIVE','DELETED')),
        content TEXT,
        policy_version TEXT NOT NULL CHECK(length(policy_version) > 0),
        classifier_version TEXT NOT NULL CHECK(length(classifier_version) > 0),
        model_id TEXT NOT NULL CHECK(length(model_id) > 0),
        model_digest TEXT NOT NULL CHECK(length(model_digest) > 0),
        prompt_version TEXT NOT NULL CHECK(length(prompt_version) > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(character_id,id),
        UNIQUE(character_id,id,kind),
        CHECK((status = 'DELETED' AND content IS NULL)
           OR (status != 'DELETED' AND content IS NOT NULL AND json_valid(content)))
    )""",
    """CREATE TABLE episodic_versions (
        character_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        content_version INTEGER NOT NULL CHECK(content_version > 0),
        content TEXT CHECK(content IS NULL OR json_valid(content)),
        sources TEXT NOT NULL CHECK(json_valid(sources) AND json_array_length(sources) > 0),
        stamp TEXT NOT NULL CHECK(json_valid(stamp)),
        created_at TEXT NOT NULL,
        PRIMARY KEY(character_id,record_id,content_version),
        FOREIGN KEY(character_id,record_id)
          REFERENCES episodic_records(character_id,id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE episodic_links (
        id TEXT PRIMARY KEY,
        character_id TEXT NOT NULL,
        episode_id TEXT NOT NULL,
        episode_kind TEXT NOT NULL DEFAULT 'EPISODE' CHECK(episode_kind = 'EPISODE'),
        episode_version INTEGER NOT NULL CHECK(episode_version > 0),
        fact_id TEXT NOT NULL,
        fact_kind TEXT NOT NULL DEFAULT 'FACT' CHECK(fact_kind = 'FACT'),
        fact_version INTEGER NOT NULL CHECK(fact_version > 0),
        sources TEXT NOT NULL CHECK(json_valid(sources) AND json_array_length(sources) > 0),
        valid INTEGER NOT NULL CHECK(valid IN (0,1)),
        created_at TEXT NOT NULL,
        FOREIGN KEY(character_id,episode_id,episode_kind)
          REFERENCES episodic_records(character_id,id,kind),
        FOREIGN KEY(character_id,fact_id,fact_kind)
          REFERENCES episodic_records(character_id,id,kind),
        FOREIGN KEY(character_id,episode_id,episode_version)
          REFERENCES episodic_versions(character_id,record_id,content_version),
        FOREIGN KEY(character_id,fact_id,fact_version)
          REFERENCES episodic_versions(character_id,record_id,content_version)
    )""",
    """CREATE TABLE episodic_merges (
        id TEXT PRIMARY KEY,
        character_id TEXT NOT NULL,
        source_fact_id TEXT NOT NULL,
        source_kind TEXT NOT NULL DEFAULT 'FACT' CHECK(source_kind = 'FACT'),
        source_version INTEGER NOT NULL CHECK(source_version > 0),
        target_fact_id TEXT NOT NULL,
        target_kind TEXT NOT NULL DEFAULT 'FACT' CHECK(target_kind = 'FACT'),
        target_version INTEGER NOT NULL CHECK(target_version > 0),
        conversation_id TEXT NOT NULL,
        policy TEXT NOT NULL CHECK(length(policy) > 0),
        evidence TEXT NOT NULL CHECK(json_valid(evidence) AND json_array_length(evidence) > 0),
        valid INTEGER NOT NULL CHECK(valid IN (0,1)),
        created_at TEXT NOT NULL,
        CHECK(source_fact_id != target_fact_id),
        FOREIGN KEY(character_id,source_fact_id,source_kind)
          REFERENCES episodic_records(character_id,id,kind),
        FOREIGN KEY(character_id,target_fact_id,target_kind)
          REFERENCES episodic_records(character_id,id,kind),
        FOREIGN KEY(character_id,source_fact_id,source_version)
          REFERENCES episodic_versions(character_id,record_id,content_version),
        FOREIGN KEY(character_id,target_fact_id,target_version)
          REFERENCES episodic_versions(character_id,record_id,content_version)
    )""",
    """CREATE TABLE episodic_receipts (
        character_id TEXT NOT NULL,
        receipt_key TEXT NOT NULL,
        record_id TEXT NOT NULL,
        operation TEXT NOT NULL CHECK(operation IN ('CREATE','UPDATE','DELETE')),
        result_version INTEGER NOT NULL CHECK(result_version > 0),
        created_at TEXT NOT NULL,
        PRIMARY KEY(character_id,receipt_key),
        FOREIGN KEY(character_id,record_id)
          REFERENCES episodic_records(character_id,id) ON DELETE RESTRICT
    )""",
    """CREATE INDEX idx_episodic_thread
       ON episodic_records(character_id,conversation_id,kind,status)""",
    """CREATE INDEX idx_episodic_fact_links
       ON episodic_links(character_id,fact_id,valid)""",
    """CREATE UNIQUE INDEX idx_episodic_active_link
       ON episodic_links(character_id,episode_id,episode_version,fact_id,fact_version,sources)
       WHERE valid = 1""",
    """CREATE UNIQUE INDEX idx_episodic_one_merge_target
       ON episodic_merges(character_id,source_fact_id) WHERE valid = 1""",
    """CREATE TRIGGER episodic_link_current BEFORE INSERT ON episodic_links
       WHEN NEW.valid = 1 BEGIN
       SELECT CASE WHEN NOT EXISTS(
          SELECT 1 FROM episodic_records e, episodic_records f
          WHERE e.character_id = NEW.character_id AND e.id = NEW.episode_id
          AND f.character_id = NEW.character_id AND f.id = NEW.fact_id
          AND e.status = 'ACTIVE' AND f.status = 'ACTIVE'
          AND e.content_version = NEW.episode_version AND f.content_version = NEW.fact_version
       ) THEN RAISE(ABORT,'stale episodic link') END;
       END""",
    """CREATE TRIGGER episodic_merge_current BEFORE INSERT ON episodic_merges
       WHEN NEW.valid = 1 BEGIN
       SELECT CASE WHEN NOT EXISTS(
          SELECT 1 FROM episodic_records s, episodic_records t
          WHERE s.character_id = NEW.character_id AND s.id = NEW.source_fact_id
          AND t.character_id = NEW.character_id AND t.id = NEW.target_fact_id
          AND s.status = 'ACTIVE' AND t.status = 'ACTIVE'
          AND s.content_version = NEW.source_version AND t.content_version = NEW.target_version
          AND s.conversation_id = NEW.conversation_id AND t.conversation_id = NEW.conversation_id
       ) THEN RAISE(ABORT,'stale or out-of-scope Fact merge') END;
       WITH RECURSIVE targets(id) AS (
          SELECT NEW.target_fact_id
          UNION
          SELECT m.target_fact_id FROM episodic_merges m JOIN targets x ON m.source_fact_id = x.id
          WHERE m.character_id = NEW.character_id AND m.valid = 1
       )
       SELECT CASE WHEN EXISTS(SELECT 1 FROM targets WHERE id = NEW.source_fact_id)
          THEN RAISE(ABORT,'cyclic Fact merge') END;
       END""",
    """CREATE TRIGGER episodic_record_no_revival BEFORE UPDATE ON episodic_records
       WHEN OLD.status = 'DELETED' AND (NEW.status != 'DELETED' OR NEW.content IS NOT NULL)
       BEGIN SELECT RAISE(ABORT,'deleted record cannot be revived'); END""",
    """CREATE TRIGGER episodic_record_identity BEFORE UPDATE ON episodic_records
       WHEN OLD.id != NEW.id OR OLD.character_id != NEW.character_id OR OLD.kind != NEW.kind
         OR OLD.conversation_id IS NOT NEW.conversation_id
       BEGIN SELECT RAISE(ABORT,'record identity is immutable'); END""",
    """CREATE TRIGGER episodic_link_immutable BEFORE UPDATE ON episodic_links
       WHEN OLD.id != NEW.id OR OLD.character_id != NEW.character_id
         OR OLD.episode_id != NEW.episode_id OR OLD.episode_version != NEW.episode_version
         OR OLD.fact_id != NEW.fact_id OR OLD.fact_version != NEW.fact_version
         OR OLD.sources != NEW.sources
       BEGIN SELECT RAISE(ABORT,'link evidence is immutable'); END""",
    """CREATE TRIGGER episodic_merge_immutable BEFORE UPDATE ON episodic_merges
       WHEN OLD.id != NEW.id OR OLD.character_id != NEW.character_id
         OR OLD.source_fact_id != NEW.source_fact_id OR OLD.source_version != NEW.source_version
         OR OLD.target_fact_id != NEW.target_fact_id OR OLD.target_version != NEW.target_version
         OR OLD.conversation_id != NEW.conversation_id OR OLD.policy != NEW.policy
         OR OLD.evidence != NEW.evidence
       BEGIN SELECT RAISE(ABORT,'merge evidence is immutable'); END""",
    # 旧関係の再有効化は禁止。再評価は新しいID・根拠を持つ関係を作る。
    """CREATE TRIGGER episodic_link_no_revival BEFORE UPDATE OF valid ON episodic_links
       WHEN OLD.valid = 0 AND NEW.valid = 1
       BEGIN SELECT RAISE(ABORT,'invalid link cannot be revived'); END""",
    """CREATE TRIGGER episodic_merge_no_revival BEFORE UPDATE OF valid ON episodic_merges
       WHEN OLD.valid = 0 AND NEW.valid = 1
       BEGIN SELECT RAISE(ABORT,'invalid merge cannot be revived'); END""",
    """CREATE TRIGGER episodic_record_invalidate AFTER UPDATE OF content_version,status ON episodic_records
       WHEN OLD.content_version != NEW.content_version OR NEW.status != 'ACTIVE'
       BEGIN
          UPDATE episodic_links SET valid = 0 WHERE character_id = NEW.character_id
             AND (episode_id = NEW.id OR fact_id = NEW.id);
          UPDATE episodic_merges SET valid = 0 WHERE character_id = NEW.character_id
             AND (source_fact_id = NEW.id OR target_fact_id = NEW.id);
       END""",
)


def initialize_episodic_schema(connection: sqlite3.Connection) -> None:
    for definition in DEFINITIONS:
        connection.execute(definition)


def validate_episodic_schema(connection: sqlite3.Connection) -> None:
    """同じtable名だけの不完全なDBを受理しない。修復・書換えは行わない。"""
    expected = sqlite3.connect(":memory:")
    try:
        initialize_episodic_schema(expected)
        rows = expected.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
        for object_type, name, definition in rows:
            actual = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
                (object_type, name),
            ).fetchone()
            if actual is None or " ".join(actual[0].split()) != " ".join(definition.split()):
                raise ValueError("existing episodic database has an unknown schema")
    finally:
        expected.close()

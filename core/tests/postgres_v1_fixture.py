"""Frozen PostgreSQL v1 DDL for synthetic migration tests."""

V1_DDL = (
    """CREATE TABLE schema_version (version INTEGER PRIMARY KEY)""",
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
        private_mode BOOLEAN NOT NULL DEFAULT false,
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
    """CREATE TABLE memory_jobs (
        seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
        id TEXT NOT NULL, binding TEXT NOT NULL, sources TEXT NOT NULL, versions TEXT NOT NULL,
        state TEXT NOT NULL, PRIMARY KEY(binding,id)
    )""",
    """CREATE TABLE memories (
        seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
        id TEXT NOT NULL, binding TEXT NOT NULL, job TEXT NOT NULL, kind TEXT NOT NULL,
        body TEXT, state TEXT NOT NULL, revoked_by TEXT,
        PRIMARY KEY(binding,id), FOREIGN KEY(binding,job) REFERENCES memory_jobs(binding,id)
    )""",
    """CREATE TABLE memory_sources (
        seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
        binding TEXT NOT NULL, memory TEXT NOT NULL, conversation TEXT NOT NULL,
        revision INTEGER NOT NULL, position INTEGER NOT NULL, epoch INTEGER NOT NULL,
        PRIMARY KEY(binding,memory,conversation,revision,position),
        FOREIGN KEY(binding,memory) REFERENCES memories(binding,id) ON DELETE CASCADE
    )""",
    """CREATE INDEX memory_source_lookup ON memory_sources(binding,conversation)""",
    """INSERT INTO schema_version(version) VALUES (1)""",
)

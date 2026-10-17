"""Frozen PostgreSQL v5 DDL for removal migration regression tests."""

V5_DDL = (
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
        private_mode BOOLEAN NOT NULL DEFAULT false, stated_at TIMESTAMPTZ,
        memory_confirmation TEXT NOT NULL DEFAULT '{}',
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
    """ALTER TABLE memory_events ADD CONSTRAINT memory_events_binding_id_key UNIQUE
        (binding,id)""",
    """CREATE TABLE memory_episodes ("seq" BIGINT GENERATED ALWAYS AS IDENTITY, "binding"
        text NOT NULL, "id" text NOT NULL, "version" integer NOT NULL, "state" text NOT NULL,
        "created_at" timestamp with time zone NOT NULL, "normalized_text" text,
        "last_user_mentioned_at" timestamp with time zone, "five_w" jsonb, "experience_time"
        jsonb, "experienced_at" timestamp with time zone, "context" text NOT NULL,
        "time_start" timestamp with time zone, "time_end" timestamp with time zone,
        "time_precision" text, CONSTRAINT memory_episodes_seq_key UNIQUE (seq), CONSTRAINT
        memory_episodes_pkey PRIMARY KEY (binding, id), CONSTRAINT memory_episodes_state_check
        CHECK ((state = ANY (ARRAY['active'::text, 'suspended'::text]))), CONSTRAINT
        memory_episodes_version_check CHECK ((version > 0)), CONSTRAINT
        memory_episodes_binding_id_version_key UNIQUE (binding, id, version), CONSTRAINT
        memory_episodes_time_precision_check CHECK ((time_precision = ANY (ARRAY['year'::text,
        'month'::text, 'day'::text, 'hour'::text, 'minute'::text, 'second'::text]))),
        CONSTRAINT memory_episodes_time_range_check CHECK ((((time_start IS NULL) AND
        (time_end IS NULL)) OR ((time_start IS NOT NULL) AND (time_end IS NOT NULL) AND
        (time_start < time_end)))), CONSTRAINT memory_episodes_context_check CHECK ((context =
        ANY (ARRAY['actual'::text, 'hypothetical'::text, 'fiction'::text]))))""",
    """CREATE TABLE memory_facts ("seq" BIGINT GENERATED ALWAYS AS IDENTITY, "binding" text
        NOT NULL, "id" text NOT NULL, "version" integer NOT NULL, "state" text NOT NULL,
        "created_at" timestamp with time zone NOT NULL, CONSTRAINT memory_facts_seq_key UNIQUE
        (seq), CONSTRAINT memory_facts_pkey PRIMARY KEY (binding, id), CONSTRAINT
        memory_facts_state_check CHECK ((state = ANY (ARRAY['active'::text,
        'suspended'::text]))), CONSTRAINT memory_facts_version_check CHECK ((version > 0)))""",
    """CREATE TABLE memory_fact_versions ("seq" BIGINT GENERATED ALWAYS AS IDENTITY,
        "binding" text NOT NULL, "id" text NOT NULL, "version" integer NOT NULL, "state" text
        NOT NULL, "created_at" timestamp with time zone NOT NULL, "normalized_text" text,
        "last_user_mentioned_at" timestamp with time zone, "five_w" jsonb, "target_time"
        jsonb, "time_start" timestamp with time zone, "time_end" timestamp with time zone,
        "time_precision" text, CONSTRAINT memory_fact_versions_seq_key UNIQUE (seq),
        CONSTRAINT memory_fact_versions_pkey PRIMARY KEY (binding, id, version), CONSTRAINT
        memory_fact_versions_state_check CHECK ((state = ANY (ARRAY['active'::text,
        'suspended'::text]))), CONSTRAINT memory_fact_versions_version_check CHECK ((version >
        0)), CONSTRAINT memory_fact_versions_time_precision_check CHECK ((time_precision = ANY
        (ARRAY['year'::text, 'month'::text, 'day'::text, 'hour'::text, 'minute'::text,
        'second'::text]))), CONSTRAINT memory_fact_versions_time_range_check CHECK
        ((((time_start IS NULL) AND (time_end IS NULL)) OR ((time_start IS NOT NULL) AND
        (time_end IS NOT NULL) AND (time_start < time_end)))), CONSTRAINT
        memory_fact_versions_fact_fkey FOREIGN KEY (binding, id) REFERENCES
        memory_facts(binding, id))""",
    """CREATE TABLE memory_semantics ("seq" BIGINT GENERATED ALWAYS AS IDENTITY, "binding"
        text NOT NULL, "id" text NOT NULL, "version" integer NOT NULL, "state" text NOT NULL,
        "created_at" timestamp with time zone NOT NULL, "normalized_text" text,
        "last_user_mentioned_at" timestamp with time zone, "formation_type" text NOT NULL,
        "proposition" jsonb, "applicability" jsonb, "time_start" timestamp with time zone,
        "time_end" timestamp with time zone, "time_precision" text, CONSTRAINT
        memory_semantics_seq_key UNIQUE (seq), CONSTRAINT memory_semantics_pkey PRIMARY KEY
        (binding, id), CONSTRAINT memory_semantics_state_check CHECK ((state = ANY
        (ARRAY['active'::text, 'suspended'::text]))), CONSTRAINT
        memory_semantics_version_check CHECK ((version > 0)), CONSTRAINT
        memory_semantics_binding_id_version_key UNIQUE (binding, id, version), CONSTRAINT
        memory_semantics_time_precision_check CHECK ((time_precision = ANY
        (ARRAY['year'::text, 'month'::text, 'day'::text, 'hour'::text, 'minute'::text,
        'second'::text]))), CONSTRAINT memory_semantics_time_range_check CHECK ((((time_start
        IS NULL) AND (time_end IS NULL)) OR ((time_start IS NOT NULL) AND (time_end IS NOT
        NULL) AND (time_start < time_end)))), CONSTRAINT memory_semantics_formation_type_check
        CHECK ((formation_type = ANY (ARRAY['direct_extraction'::text,
        'experience_derived'::text]))))""",
    """CREATE TABLE memory_episode_fact_links ("seq" BIGINT GENERATED ALWAYS AS IDENTITY,
        "binding" text NOT NULL, "id" text NOT NULL, "version" integer NOT NULL, "state" text
        NOT NULL, "created_at" timestamp with time zone NOT NULL, "episode" text NOT NULL,
        "episode_version" integer NOT NULL, "fact" text NOT NULL, "fact_version" integer NOT
        NULL, CONSTRAINT memory_episode_fact_links_seq_key UNIQUE (seq), CONSTRAINT
        memory_episode_fact_links_pkey PRIMARY KEY (binding, id), CONSTRAINT
        memory_episode_fact_links_state_check CHECK ((state = ANY (ARRAY['active'::text,
        'suspended'::text]))), CONSTRAINT memory_episode_fact_links_version_check CHECK
        ((version > 0)), CONSTRAINT memory_episode_fact_links_binding_id_version_key UNIQUE
        (binding, id, version), CONSTRAINT memory_episode_fact_links_episode_fkey FOREIGN KEY
        (binding, episode, episode_version) REFERENCES memory_episodes(binding, id, version),
        CONSTRAINT memory_episode_fact_links_fact_fkey FOREIGN KEY (binding, fact,
        fact_version) REFERENCES memory_fact_versions(binding, id, version))""",
    """CREATE TABLE memory_semantic_episodes ("seq" BIGINT GENERATED ALWAYS AS IDENTITY,
        "binding" text NOT NULL, "semantic" text NOT NULL, "semantic_version" integer NOT
        NULL, "episode" text NOT NULL, "episode_version" integer NOT NULL, CONSTRAINT
        memory_semantic_episodes_seq_key UNIQUE (seq), CONSTRAINT
        memory_semantic_episodes_pkey PRIMARY KEY (binding, semantic, semantic_version,
        episode, episode_version), CONSTRAINT memory_semantic_episodes_semantic_fkey FOREIGN
        KEY (binding, semantic, semantic_version) REFERENCES memory_semantics(binding, id,
        version), CONSTRAINT memory_semantic_episodes_episode_fkey FOREIGN KEY (binding,
        episode, episode_version) REFERENCES memory_episodes(binding, id, version))""",
    """CREATE TABLE memory_record_citations ("seq" BIGINT GENERATED ALWAYS AS IDENTITY,
        "binding" text NOT NULL, "record_kind" text NOT NULL, "record_id" text NOT NULL,
        "version" integer NOT NULL, "episode" text, "fact" text, "semantic" text,
        "conversation" text NOT NULL, "revision" integer NOT NULL, "position" integer NOT
        NULL, "epoch" integer NOT NULL, "speaker" text NOT NULL, "citation_role" text NOT
        NULL, "start_offset" integer NOT NULL, "end_offset" integer NOT NULL, CONSTRAINT
        memory_record_citations_seq_key UNIQUE (seq), CONSTRAINT memory_record_citations_pkey
        PRIMARY KEY (binding, record_kind, record_id, version, conversation, revision,
        "position", epoch, speaker, citation_role, start_offset, end_offset), CONSTRAINT
        memory_record_citations_record_kind_check CHECK ((record_kind = ANY
        (ARRAY['episode'::text, 'fact'::text, 'semantic'::text]))), CONSTRAINT
        memory_record_citations_episode_fkey FOREIGN KEY (binding, episode, version)
        REFERENCES memory_episodes(binding, id, version), CONSTRAINT
        memory_record_citations_fact_fkey FOREIGN KEY (binding, fact, version) REFERENCES
        memory_fact_versions(binding, id, version), CONSTRAINT
        memory_record_citations_semantic_fkey FOREIGN KEY (binding, semantic, version)
        REFERENCES memory_semantics(binding, id, version), CONSTRAINT
        memory_record_citations_owner_check CHECK ((((record_kind = 'episode'::text) AND
        (episode IS NOT NULL) AND (episode = record_id) AND (fact IS NULL) AND (semantic IS
        NULL)) OR ((record_kind = 'fact'::text) AND (fact IS NOT NULL) AND (fact = record_id)
        AND (episode IS NULL) AND (semantic IS NULL)) OR ((record_kind = 'semantic'::text) AND
        (semantic IS NOT NULL) AND (semantic = record_id) AND (episode IS NULL) AND (fact IS
        NULL)))), CONSTRAINT memory_record_citations_version_check CHECK ((version > 0)),
        CONSTRAINT memory_record_citations_citation_role_check CHECK ((citation_role = ANY
        (ARRAY['record'::text, 'reason'::text]))), CONSTRAINT
        memory_record_citations_speaker_check CHECK ((speaker = ANY (ARRAY['user'::text,
        'assistant'::text, 'system'::text, 'developer'::text, 'tool'::text]))), CONSTRAINT
        memory_record_citations_revision_check CHECK ((revision > 0)), CONSTRAINT
        memory_record_citations_position_check CHECK (("position" >= 0)), CONSTRAINT
        memory_record_citations_epoch_check CHECK ((epoch >= 0)), CONSTRAINT
        memory_record_citations_offsets_check CHECK (((start_offset >= 0) AND (end_offset >
        start_offset))))""",
    """CREATE TABLE memory_event_records ("seq" BIGINT GENERATED ALWAYS AS IDENTITY,
        "binding" text NOT NULL, "event" text NOT NULL, "record_kind" text NOT NULL,
        "record_id" text NOT NULL, "version" integer NOT NULL, "episode" text, "fact" text,
        "semantic" text, "link" text, CONSTRAINT memory_event_records_seq_key UNIQUE (seq),
        CONSTRAINT memory_event_records_pkey PRIMARY KEY (binding, event, record_kind,
        record_id, version), CONSTRAINT memory_event_records_record_kind_check CHECK
        ((record_kind = ANY (ARRAY['episode'::text, 'fact'::text, 'semantic'::text,
        'episode_fact_link'::text]))), CONSTRAINT memory_event_records_episode_fkey FOREIGN
        KEY (binding, episode, version) REFERENCES memory_episodes(binding, id, version),
        CONSTRAINT memory_event_records_fact_fkey FOREIGN KEY (binding, fact, version)
        REFERENCES memory_fact_versions(binding, id, version), CONSTRAINT
        memory_event_records_semantic_fkey FOREIGN KEY (binding, semantic, version) REFERENCES
        memory_semantics(binding, id, version), CONSTRAINT memory_event_records_link_fkey
        FOREIGN KEY (binding, link, version) REFERENCES memory_episode_fact_links(binding, id,
        version), CONSTRAINT memory_event_records_owner_check CHECK ((((record_kind =
        'episode'::text) AND (episode IS NOT NULL) AND (episode = record_id) AND (fact IS
        NULL) AND (semantic IS NULL) AND (link IS NULL)) OR ((record_kind = 'fact'::text) AND
        (fact IS NOT NULL) AND (fact = record_id) AND (episode IS NULL) AND (semantic IS NULL)
        AND (link IS NULL)) OR ((record_kind = 'semantic'::text) AND (semantic IS NOT NULL)
        AND (semantic = record_id) AND (episode IS NULL) AND (fact IS NULL) AND (link IS
        NULL)) OR ((record_kind = 'episode_fact_link'::text) AND (link IS NOT NULL) AND (link
        = record_id) AND (episode IS NULL) AND (fact IS NULL) AND (semantic IS NULL)))),
        CONSTRAINT memory_event_records_version_check CHECK ((version > 0)), CONSTRAINT
        memory_event_records_event_fkey FOREIGN KEY (binding, event) REFERENCES
        memory_events(binding, id))""",
    """CREATE TABLE memory_record_registrations ("seq" BIGINT GENERATED ALWAYS AS IDENTITY,
        "binding" text NOT NULL, "id" text NOT NULL, "request_digest" text, "results" jsonb
        NOT NULL, CONSTRAINT memory_record_registrations_seq_key UNIQUE (seq), CONSTRAINT
        memory_record_registrations_pkey PRIMARY KEY (binding, id))""",
    """INSERT INTO schema_version(version) VALUES (5)""",
)

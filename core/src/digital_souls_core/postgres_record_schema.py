"""Canonical record v5 DDL and exact column/constraint descriptors."""

# (SQL type, nullable); defaults are absent except the existing seq identity.
SEQ = ("bigint", False)
TEXT = ("text", False)
INT = ("integer", False)
TIME = ("timestamp with time zone", True)
JSON = ("jsonb", True)
BASE = {
    "seq": SEQ,
    "binding": TEXT,
    "id": TEXT,
    "version": INT,
    "state": TEXT,
    "created_at": ("timestamp with time zone", False),
}
BODY = {"normalized_text": ("text", True), "last_user_mentioned_at": TIME}
RANGE = {"time_start": TIME, "time_end": TIME, "time_precision": ("text", True)}
COLUMNS = {
    "memory_episodes": {
        **BASE,
        **BODY,
        "five_w": JSON,
        "experience_time": JSON,
        "experienced_at": TIME,
        "context": TEXT,
        **RANGE,
    },
    "memory_facts": dict(BASE),
    "memory_fact_versions": {**BASE, **BODY, "five_w": JSON, "target_time": JSON, **RANGE},
    "memory_semantics": {
        **BASE,
        **BODY,
        "formation_type": TEXT,
        "proposition": JSON,
        "applicability": JSON,
        **RANGE,
    },
    "memory_episode_fact_links": {
        **BASE,
        "episode": TEXT,
        "episode_version": INT,
        "fact": TEXT,
        "fact_version": INT,
    },
    "memory_semantic_episodes": {
        "seq": SEQ,
        "binding": TEXT,
        "semantic": TEXT,
        "semantic_version": INT,
        "episode": TEXT,
        "episode_version": INT,
    },
    "memory_record_citations": {
        "seq": SEQ,
        "binding": TEXT,
        "record_kind": TEXT,
        "record_id": TEXT,
        "version": INT,
        "episode": ("text", True),
        "fact": ("text", True),
        "semantic": ("text", True),
        "conversation": TEXT,
        "revision": INT,
        "position": INT,
        "epoch": INT,
        "speaker": TEXT,
        "citation_role": TEXT,
        "start_offset": INT,
        "end_offset": INT,
    },
    "memory_event_records": {
        "seq": SEQ,
        "binding": TEXT,
        "event": TEXT,
        "record_kind": TEXT,
        "record_id": TEXT,
        "version": INT,
        "episode": ("text", True),
        "fact": ("text", True),
        "semantic": ("text", True),
        "link": ("text", True),
    },
    "memory_record_registrations": {
        "seq": SEQ,
        "binding": TEXT,
        "id": TEXT,
        "request_digest": ("text", True),
        "results": ("jsonb", False),
    },
}
CONSTRAINTS: dict[tuple[str, str], str] = {}


def constraint(table: str, suffix: str, definition: str) -> None:
    CONSTRAINTS[table, table + "_" + suffix] = definition


def check(table: str, column: str, expression: str) -> None:
    constraint(table, column + "_check", "CHECK (" + expression + ")")


def choices(table: str, column: str, values: tuple[str, ...]) -> None:
    array = ", ".join("'" + v + "'::text" for v in values)
    check(table, column, "(" + column + " = ANY (ARRAY[" + array + "]))")


def fk(table: str, suffix: str, columns: str, target: str, target_columns: str) -> None:
    constraint(
        table,
        suffix,
        "FOREIGN KEY (" + columns + ") REFERENCES " + target + "(" + target_columns + ")",
    )


for table in COLUMNS:
    constraint(table, "seq_key", "UNIQUE (seq)")
    if table in {"memory_semantic_episodes", "memory_record_citations", "memory_event_records"}:
        pk = {
            "memory_semantic_episodes": (
                "binding, semantic, semantic_version, episode, episode_version"
            ),
            "memory_record_citations": (
                "binding, record_kind, record_id, version, conversation, "
                'revision, "position", epoch, speaker, citation_role, '
                "start_offset, end_offset"
            ),
            "memory_event_records": "binding, event, record_kind, record_id, version",
        }[table]
    else:
        pk = "binding, id, version" if table == "memory_fact_versions" else "binding, id"
    constraint(table, "pkey", "PRIMARY KEY (" + pk + ")")
    if "state" in COLUMNS[table]:
        choices(table, "state", ("active", "suspended"))
        check(table, "version", "(version > 0)")
    if table in {"memory_episodes", "memory_semantics", "memory_episode_fact_links"}:
        constraint(table, "binding_id_version_key", "UNIQUE (binding, id, version)")
    if "time_precision" in COLUMNS[table]:
        choices(table, "time_precision", ("year", "month", "day", "hour", "minute", "second"))
        check(
            table,
            "time_range",
            (
                "(((time_start IS NULL) AND (time_end IS NULL)) OR ((time_start "
                "IS NOT NULL) AND (time_end IS NOT NULL) AND (time_start < "
                "time_end)))"
            ),
        )

choices("memory_episodes", "context", ("actual", "hypothetical", "fiction"))
choices("memory_semantics", "formation_type", ("direct_extraction", "experience_derived"))
fk("memory_fact_versions", "fact_fkey", "binding, id", "memory_facts", "binding, id")
fk(
    "memory_episode_fact_links",
    "episode_fkey",
    "binding, episode, episode_version",
    "memory_episodes",
    "binding, id, version",
)
fk(
    "memory_episode_fact_links",
    "fact_fkey",
    "binding, fact, fact_version",
    "memory_fact_versions",
    "binding, id, version",
)
fk(
    "memory_semantic_episodes",
    "semantic_fkey",
    "binding, semantic, semantic_version",
    "memory_semantics",
    "binding, id, version",
)
fk(
    "memory_semantic_episodes",
    "episode_fkey",
    "binding, episode, episode_version",
    "memory_episodes",
    "binding, id, version",
)
for table in ("memory_record_citations", "memory_event_records"):
    kinds = ("episode", "fact", "semantic") + (
        ("episode_fact_link",) if table == "memory_event_records" else ()
    )
    choices(table, "record_kind", kinds)
    parts = []
    for kind in kinds:
        col = "link" if kind == "episode_fact_link" else kind
        terms = [
            "(record_kind = '" + kind + "'::text)",
            "(" + col + " IS NOT NULL)",
            "(" + col + " = record_id)",
        ]
        terms.extend(
            "(" + other + " IS NULL)"
            for other in ("episode", "fact", "semantic", "link")
            if other in COLUMNS[table] and other != col
        )
        parts.append("(" + " AND ".join(terms) + ")")
        target = {
            "episode": "memory_episodes",
            "fact": "memory_fact_versions",
            "semantic": "memory_semantics",
            "link": "memory_episode_fact_links",
        }[col]
        fk(table, col + "_fkey", "binding, " + col + ", version", target, "binding, id, version")
    check(table, "owner", "(" + " OR ".join(parts) + ")")
    check(table, "version", "(version > 0)")
choices("memory_record_citations", "citation_role", ("record", "reason"))
choices("memory_record_citations", "speaker", ("user", "assistant", "system", "developer", "tool"))
for col, expr in [
    ("revision", "(revision > 0)"),
    ("position", '("position" >= 0)'),
    ("epoch", "(epoch >= 0)"),
    ("offsets", "((start_offset >= 0) AND (end_offset > start_offset))"),
]:
    check("memory_record_citations", col, expr)
# Composite event ownership makes cross-Binding event references impossible.
CONSTRAINTS["memory_events", "memory_events_binding_id_key"] = "UNIQUE (binding, id)"
fk("memory_event_records", "event_fkey", "binding, event", "memory_events", "binding, id")


def _ddl() -> tuple[str, ...]:
    statements = [
        "ALTER TABLE memory_events ADD CONSTRAINT memory_events_binding_id_key UNIQUE (binding,id)"
    ]
    for table, columns in COLUMNS.items():
        fields = []
        for column, (kind, nullable) in columns.items():
            fields.append(
                '"'
                + column
                + '" '
                + (
                    "BIGINT GENERATED ALWAYS AS IDENTITY"
                    if column == "seq"
                    else kind + ("" if nullable else " NOT NULL")
                )
            )
        fields.extend(
            "CONSTRAINT " + name + " " + definition
            for (owner, name), definition in CONSTRAINTS.items()
            if owner == table
        )
        statements.append("CREATE TABLE " + table + " (" + ", ".join(fields) + ")")
    return tuple(statements)


RECORD_DDL = _ddl()

"""Version 8 content-free job ledger, with one row per Binding/conversation."""

from .postgres_record_schema import INT, SEQ, TEXT

TIME = ("timestamp with time zone", False)
COLUMNS = {
    "formation_jobs": {
        "seq": SEQ,
        "binding": TEXT,
        "conversation": TEXT,
        "target_revision": INT,
        "state": TEXT,
        "attempts": INT,
        "leased_revision": ("integer", True),
        "lease_token": ("text", True),
        "lease_until": ("timestamp with time zone", True),
        "available_at": TIME,
        "last_code": TEXT,
        "created_at": TIME,
        "updated_at": TIME,
    }
}
CONSTRAINTS = {
    ("formation_jobs", "formation_jobs_seq_key"): "UNIQUE (seq)",
    ("formation_jobs", "formation_jobs_pkey"): "PRIMARY KEY (binding, conversation)",
    ("formation_jobs", "formation_jobs_source_fkey"): (
        "FOREIGN KEY (binding, conversation) REFERENCES conversations(binding, id) "
        "ON DELETE CASCADE"
    ),
    ("formation_jobs", "formation_jobs_state_check"): (
        "CHECK ((state = ANY (ARRAY['pending'::text, 'leased'::text, 'done'::text, "
        "'failed'::text])))"
    ),
    ("formation_jobs", "formation_jobs_revision_check"): "CHECK ((target_revision > 0))",
    ("formation_jobs", "formation_jobs_attempts_check"): "CHECK ((attempts >= 0))",
    ("formation_jobs", "formation_jobs_lease_check"): (
        "CHECK ((((state = 'leased'::text) = (lease_token IS NOT NULL)) AND "
        "((lease_token IS NULL) = (lease_until IS NULL)) AND "
        "((lease_token IS NULL) = (leased_revision IS NULL))))"
    ),
}
DDL = (
    "CREATE TABLE formation_jobs ("
    + ", ".join(
        name
        + " "
        + ("BIGINT GENERATED ALWAYS AS IDENTITY" if name == "seq" else kind)
        + ("" if nullable else " NOT NULL")
        for name, (kind, nullable) in COLUMNS["formation_jobs"].items()
    )
    + ", "
    + ", ".join(
        "CONSTRAINT " + name + " " + definition for (_, name), definition in CONSTRAINTS.items()
    )
    + ")",
)

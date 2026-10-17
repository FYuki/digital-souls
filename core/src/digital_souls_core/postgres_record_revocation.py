"""Fail-closed canonical erasure inside the existing history revocation transaction."""

from typing import Any

from psycopg import Connection, sql


def revoke_records(
    db: Connection[tuple[Any, ...]],
    binding: str,
    conversation: str,
    event: str,
    revisions: tuple[int, ...] | None = None,
) -> None:
    direct = db.execute(
        (
            "SELECT DISTINCT record_kind,record_id FROM "
            "memory_record_citations WHERE binding=%s AND conversation=%s AND "
            "(%s::integer[] IS NULL OR revision=ANY(%s))"
        ),
        (
            binding,
            conversation,
            list(revisions) if revisions is not None else None,
            list(revisions) if revisions is not None else None,
        ),
    ).fetchall()
    episodes = {identifier for kind, identifier in direct if kind == "episode"}
    facts = {identifier for kind, identifier in direct if kind == "fact"}
    semantics = {identifier for kind, identifier in direct if kind == "semantic"}
    semantics.update(
        row[0]
        for row in db.execute(
            (
                "SELECT semantic FROM memory_semantic_episodes WHERE binding=%s "
                "AND episode=ANY(%s::text[])"
            ),
            (binding, list(episodes)),
        )
    )
    links = {
        row[0]
        for row in db.execute(
            (
                "SELECT id FROM memory_episode_fact_links WHERE binding=%s AND "
                "(episode=ANY(%s::text[]) OR fact=ANY(%s::text[]))"
            ),
            (binding, list(episodes), list(facts)),
        )
    }
    for table, kind, identifiers, body in (
        (
            "memory_episodes",
            "episode",
            episodes,
            (
                "normalized_text=NULL,five_w=NULL,experience_time=NULL,"
                "experienced_at=NULL,time_start=NULL,time_end=NULL,time_precision=NULL"
            ),
        ),
        (
            "memory_fact_versions",
            "fact",
            facts,
            (
                "normalized_text=NULL,five_w=NULL,target_time=NULL,"
                "time_start=NULL,time_end=NULL,time_precision=NULL"
            ),
        ),
        (
            "memory_semantics",
            "semantic",
            semantics,
            (
                "normalized_text=NULL,proposition=NULL,applicability=NULL,"
                "time_start=NULL,time_end=NULL,time_precision=NULL"
            ),
        ),
        ("memory_episode_fact_links", "episode_fact_link", links, ""),
    ):
        owner = "link" if kind == "episode_fact_link" else kind
        # Capture every affected version under the existing event, even if a
        # previous event already suspended it. No body is needed for reevaluation.
        db.execute(
            sql.SQL(
                "INSERT INTO "
                "memory_event_records(binding,event,record_kind,record_id,version,"
                "{}) SELECT binding,%s,%s,id,version,id FROM {} WHERE binding=%s "
                "AND id=ANY(%s::text[]) ON CONFLICT DO NOTHING"
            ).format(sql.Identifier(owner), sql.Identifier(table)),
            (event, kind, binding, list(identifiers)),
        )
        assignment = "state='suspended'" + ("," + body if body else "")
        db.execute(
            sql.SQL("UPDATE {} SET {} WHERE binding=%s AND id=ANY(%s::text[])").format(
                sql.Identifier(table), sql.SQL(assignment)
            ),
            (binding, list(identifiers)),
        )
    db.execute(
        "UPDATE memory_facts SET state='suspended' WHERE binding=%s AND id=ANY(%s::text[])",
        (binding, list(facts)),
    )
    # The digest includes body-derived values. Clear it for any registration
    # returning an affected record, including dependent records and old Fact
    # versions, in the same transaction as body erasure and history revocation.
    db.execute(
        "UPDATE memory_record_registrations SET request_digest=NULL "
        "WHERE binding=%s AND request_digest IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM jsonb_array_elements(results) AS result "
        "JOIN memory_event_records affected ON affected.binding=%s AND affected.event=%s "
        "AND affected.record_kind=result->>'kind' "
        "AND affected.record_id=result->>'record_id' "
        "AND affected.version=(result->>'version')::integer)",
        (binding, binding, event),
    )

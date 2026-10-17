"""Physical erasure, temporal projection and frozen v4 migration contracts."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg import Connection, sql

from digital_souls_core.application import CoreError
from digital_souls_core.memory_record_store import MemoryRecord
from digital_souls_core.memory_records import (
    PartialDateTime,
    RecordKind,
    TemporalValue,
    TimePrecision,
)
from digital_souls_core.postgres_db import PostgresDatabase, key
from digital_souls_core.postgres_schema import SCHEMA_VERSION

from . import test_postgres_stores
from .test_memory_record_store_contract import citation, derived, episode, fact, reference
from .test_postgres_stated_at import install_v1
from .test_postgres_stores import BINDING, Stores

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


def install_v4(stores: Stores) -> None:
    install_v1(stores)
    with stores.database.transaction(BINDING) as db:
        db.execute("ALTER TABLE turns ADD COLUMN stated_at timestamptz")
        db.execute("ALTER TABLE turns ADD COLUMN memory_confirmation text NOT NULL DEFAULT '{}'")
        db.execute(
            "UPDATE turns SET stated_at='2026-10-07T00:00:00Z',memory_confirmation='{"
            + '"0":null'
            + "}'"
        )
        db.execute(
            "CREATE TABLE turn_tombstones (seq BIGINT GENERATED ALWAYS AS "
            "IDENTITY UNIQUE, binding TEXT NOT NULL, conversation TEXT NOT "
            "NULL, request TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY "
            "KEY(binding,conversation,request), FOREIGN "
            "KEY(binding,conversation) REFERENCES conversations(binding,id) "
            "ON DELETE CASCADE)"
        )
        db.execute(
            "CREATE TABLE turn_deletions (seq BIGINT GENERATED ALWAYS AS "
            "IDENTITY UNIQUE, event TEXT PRIMARY KEY, binding TEXT NOT NULL, "
            "conversation TEXT NOT NULL, turn_revisions TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO "
            "turn_tombstones(binding,conversation,request,revision) SELECT "
            "binding,id,'deleted-request',2 FROM conversations"
        )
        db.execute(
            "INSERT INTO "
            "turn_deletions(event,binding,conversation,turn_revisions) SELECT "
            "'td1',binding,id,'[2]' FROM conversations"
        )
        db.execute(
            (
                "INSERT INTO memory_jobs(binding,id,sources,versions,state) "
                "VALUES (%s,'j1','[]','v1','done')"
            ),
            (key(BINDING),),
        )
        db.execute(
            (
                "INSERT INTO memories(binding,id,job,kind,body,state) VALUES "
                "(%s,'m1','j1','semantic','synthetic legacy','active')"
            ),
            (key(BINDING),),
        )
        db.execute("UPDATE schema_version SET version=4")


def snapshot(stores: Stores) -> dict[str, list[tuple[Any, ...]]]:
    with stores.database.transaction(BINDING) as db:
        tables = [
            r[0]
            for r in db.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename",
                (stores.config.schema_name,),
            )
        ]
        return {
            t: db.execute(
                sql.SQL("SELECT * FROM {} ORDER BY seq").format(sql.Identifier(t))
            ).fetchall()
            for t in tables
            if t != "schema_version"
        }


def test_v4_migration_preserves_all_rows_and_reopens(stores: Stores) -> None:
    install_v4(stores)
    before = snapshot(stores)
    PostgresDatabase(stores.config).initialize()
    after = snapshot(stores)
    assert all(row[-1] is None for row in after["turns"])
    assert [row[:-1] for row in after["turns"]] == before["turns"]
    assert all(
        after[t] == rows
        for t, rows in before.items()
        if t not in {"memories", "memory_sources", "memory_jobs", "turns"}
    )
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
    PostgresDatabase(stores.config).initialize()


@pytest.mark.parametrize("after", ["table", "version"])
def test_v4_migration_rolls_back_and_retries(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, after: str
) -> None:
    install_v4(stores)
    before = snapshot(stores)
    original = Connection.execute

    def execute(db: Connection[tuple[Any, ...]], query: Any, *args: Any, **kwargs: Any) -> Any:
        text = query.as_string(db) if isinstance(query, sql.Composable) else str(query)
        result = original(db, query, *args, **kwargs)
        if (after == "table" and text.lstrip().upper().startswith("CREATE TABLE MEMORY_")) or (
            after == "version" and text == "UPDATE schema_version SET version=5"
        ):
            raise RuntimeError("Synthetic migration interruption")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", execute)
        with pytest.raises(RuntimeError, match="Synthetic migration interruption"):
            PostgresDatabase(stores.config).initialize()
    assert snapshot(stores) == before
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(4,)]
    PostgresDatabase(stores.config).initialize()
    migrated = snapshot(stores)
    assert all(row[-1] is None for row in migrated["turns"])
    assert [row[:-1] for row in migrated["turns"]] == before["turns"]
    assert all(
        migrated[t] == rows
        for t, rows in before.items()
        if t not in {"memories", "memory_sources", "memory_jobs", "turns"}
    )


@pytest.mark.parametrize(
    "precision,parts,start,end",
    [
        (
            TimePrecision.YEAR,
            {},
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2025, 1, 1, tzinfo=UTC),
        ),
        (
            TimePrecision.MONTH,
            {"month": 2},
            datetime(2024, 2, 1, tzinfo=UTC),
            datetime(2024, 3, 1, tzinfo=UTC),
        ),
        (
            TimePrecision.DAY,
            {"month": 2, "day": 29},
            datetime(2024, 2, 29, tzinfo=UTC),
            datetime(2024, 3, 1, tzinfo=UTC),
        ),
        (
            TimePrecision.HOUR,
            {"month": 2, "day": 29, "hour": 23},
            datetime(2024, 2, 29, 23, tzinfo=UTC),
            datetime(2024, 3, 1, tzinfo=UTC),
        ),
        (
            TimePrecision.MINUTE,
            {"month": 2, "day": 29, "hour": 23, "minute": 59},
            datetime(2024, 2, 29, 23, 59, tzinfo=UTC),
            datetime(2024, 3, 1, tzinfo=UTC),
        ),
        (
            TimePrecision.SECOND,
            {"month": 2, "day": 29, "hour": 23, "minute": 59, "second": 59},
            datetime(2024, 2, 29, 23, 59, 59, tzinfo=UTC),
            datetime(2024, 3, 1, tzinfo=UTC),
        ),
    ],
)
def test_temporal_columns_match_json_all_record_kinds(
    stores: Stores, precision: TimePrecision, parts: dict[str, int], start: datetime, end: datetime
) -> None:
    from digital_souls_core.memory_record_store import FactWrite, RecordBatch
    from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

    port = PostgresMemoryRecords(stores.database)
    time = TemporalValue(
        start=PartialDateTime(year=2024, precision=precision, **parts), timezone="UTC"
    )
    e, e2 = (
        replace(episode(citation(stores)), experience_time=time),
        episode(citation(stores), "e2"),
    )
    f = replace(fact(e.citations[0]), target_time=time)
    s = replace(derived(e, e2), applicability=time)
    port.register(
        BINDING, RecordBatch(episodes=(e, e2), facts=(FactWrite(f),), semantics=(s,)), "v1"
    )
    expected_records: dict[str, MemoryRecord] = {"e1": e, "f1": f, "s1": s}
    for table, name, identifier in [
        ("memory_episodes", "experience_time", "e1"),
        ("memory_fact_versions", "target_time", "f1"),
        ("memory_semantics", "applicability", "s1"),
    ]:
        with stores.database.transaction(BINDING) as db:
            row = db.execute(
                sql.SQL("SELECT {},time_start,time_end,time_precision FROM {} WHERE id=%s").format(
                    sql.Identifier(name), sql.Identifier(table)
                ),
                (identifier,),
            ).fetchone()
        assert row is not None and row[1:] == (start, end, precision.value)
        assert row[0]["start"]["precision"] == precision.value
        assert row[0]["start"]["year"] == 2024 and row[0]["timezone"] == "UTC"
        assert (
            port.get(BINDING, reference(expected_records[identifier]).kind, identifier)
            == expected_records[identifier]
        )


@pytest.mark.parametrize(
    "time,expected",
    [
        (TemporalValue(), (None, None, None)),
        (
            TemporalValue(start=PartialDateTime(precision=TimePrecision.MONTH, year=2024, month=2)),
            (None, None, "month"),
        ),
        (
            TemporalValue(
                start=PartialDateTime(precision=TimePrecision.MONTH, year=2024, month=2),
                end=PartialDateTime(precision=TimePrecision.MONTH, year=2024, month=3),
                timezone="Asia/Tokyo",
            ),
            (datetime(2024, 1, 31, 15, tzinfo=UTC), datetime(2024, 3, 31, 15, tzinfo=UTC), "month"),
        ),
    ],
)
def test_unknown_timezone_and_inclusive_end_projection(
    stores: Stores, time: TemporalValue, expected: tuple[object, ...]
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch
    from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

    e = replace(episode(citation(stores)), experience_time=time)
    PostgresMemoryRecords(stores.database).register(BINDING, RecordBatch(episodes=(e,)), "v1")
    with stores.database.transaction(BINDING) as db:
        assert (
            db.execute("SELECT time_start,time_end,time_precision FROM memory_episodes").fetchone()
            == expected
        )


def test_invalid_timezone_refused_atomically(stores: Stores) -> None:
    from digital_souls_core.memory_record_store import RecordBatch
    from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

    port = PostgresMemoryRecords(stores.database)
    e = replace(
        episode(citation(stores)), experience_time=TemporalValue(timezone="Unknown/Synthetic")
    )
    with pytest.raises(CoreError):
        port.register(BINDING, RecordBatch(episodes=(e,)), "v1")
    assert port.list(BINDING, RecordKind.EPISODE) == ()


def test_revoked_bodies_json_and_temporal_columns_are_null(stores: Stores) -> None:
    from digital_souls_core.memory_record_store import FactWrite, RecordBatch
    from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

    port = PostgresMemoryRecords(stores.database)
    time = TemporalValue(
        start=PartialDateTime(precision=TimePrecision.MONTH, year=2024, month=2),
        timezone="UTC",
    )
    e = replace(
        episode(citation(stores)),
        experience_time=time,
        experienced_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    e2 = episode(citation(stores), "e2")
    f = replace(fact(e.citations[0]), target_time=time)
    s = replace(derived(e, e2), applicability=time)
    port.register(
        BINDING, RecordBatch(episodes=(e, e2), facts=(FactWrite(f),), semantics=(s,)), "v1"
    )
    f2 = replace(f, version=2, citations=e2.citations)
    port.register(BINDING, RecordBatch(facts=(FactWrite(f2, expected_version=1),)), "v2")
    with stores.database.transaction(BINDING) as db:
        for table in ("memory_episodes", "memory_fact_versions", "memory_semantics"):
            rows = db.execute(
                sql.SQL("SELECT time_start,time_end FROM {} WHERE id <> 'e2'").format(
                    sql.Identifier(table)
                )
            ).fetchall()
            assert rows and all(start is not None and end is not None for start, end in rows)
    stores.history.delete(BINDING, e.citations[0].source.reference.conversation_id)
    for table, columns in [
        (
            "memory_episodes",
            (
                "normalized_text,five_w,experience_time,experienced_at,time_start,"
                "time_end,time_precision"
            ),
        ),
        (
            "memory_fact_versions",
            "normalized_text,five_w,target_time,time_start,time_end,time_precision",
        ),
        (
            "memory_semantics",
            "normalized_text,proposition,applicability,time_start,time_end,time_precision",
        ),
    ]:
        with stores.database.transaction(BINDING) as db:
            rows = db.execute(
                sql.SQL("SELECT {} FROM {} WHERE state='suspended'").format(
                    sql.SQL(columns), sql.Identifier(table)
                )
            ).fetchall()
            assert rows and all(all(v is None for v in row) for row in rows)
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT count(*) FROM memory_event_records").fetchone() == (4,)
        row = db.execute("SELECT count(*) FROM memory_record_citations").fetchone()
        assert row is not None and row[0] > 0


@pytest.mark.parametrize(
    "alteration",
    [
        "ALTER TABLE memory_episodes ALTER COLUMN five_w TYPE text USING five_w::text",
        "ALTER TABLE memory_facts DROP CONSTRAINT memory_facts_state_check",
        (
            "ALTER TABLE memory_semantic_episodes DROP CONSTRAINT "
            "memory_semantic_episodes_episode_fkey"
        ),
        "ALTER TABLE memory_record_citations ADD COLUMN unexpected text",
        "ALTER TABLE memory_episodes ALTER COLUMN time_start SET DEFAULT now()",
    ],
)
def test_record_schema_drift_rejected(stores: Stores, alteration: str) -> None:
    with stores.database.transaction(BINDING) as db:
        db.execute(alteration)
    with pytest.raises(CoreError) as error:
        PostgresDatabase(stores.config).initialize()
    assert error.value.code == "storage_schema"


def test_registration_failure_after_sql_write_is_atomic(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    from digital_souls_core.memory_record_store import FactWrite, RecordBatch
    from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

    port = PostgresMemoryRecords(stores.database)
    e, e2 = episode(citation(stores)), episode(citation(stores), "e2")
    batch = RecordBatch(
        episodes=(e, e2), facts=(FactWrite(fact(e.citations[0])),), semantics=(derived(e, e2),)
    )
    original = Connection.execute

    def interrupted(db: Connection[tuple[Any, ...]], query: Any, *args: Any, **kwargs: Any) -> Any:
        text = query.as_string(db) if isinstance(query, sql.Composable) else str(query)
        result = original(db, query, *args, **kwargs)
        if text.startswith('INSERT INTO "memory_semantics"'):
            raise RuntimeError("Synthetic registration interruption")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", interrupted)
        with pytest.raises(RuntimeError, match="Synthetic registration interruption"):
            port.register(BINDING, batch, "v1")
    with stores.database.transaction(BINDING) as db:
        for table in (
            "memory_episodes",
            "memory_facts",
            "memory_fact_versions",
            "memory_semantics",
            "memory_record_citations",
            "memory_semantic_episodes",
            "memory_record_registrations",
        ):
            assert db.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone() == (0,)
    port.register(BINDING, batch, "v1")


def test_revocation_failure_rolls_back_history_event_and_canonical_body(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch
    from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

    port = PostgresMemoryRecords(stores.database)
    e = episode(citation(stores))
    port.register(BINDING, RecordBatch(episodes=(e,)), "v1")
    cid = e.citations[0].source.reference.conversation_id
    before = stores.history.read(BINDING, cid)
    original = Connection.execute

    def interrupted(db: Connection[tuple[Any, ...]], query: Any, *args: Any, **kwargs: Any) -> Any:
        text = query.as_string(db) if isinstance(query, sql.Composable) else str(query)
        result = original(db, query, *args, **kwargs)
        if text.startswith('UPDATE "memory_episodes"'):
            raise RuntimeError("Synthetic revocation interruption")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", interrupted)
        with pytest.raises(RuntimeError, match="Synthetic revocation interruption"):
            stores.history.delete(BINDING, cid)
    assert stores.history.read(BINDING, cid) == before
    assert port.get(BINDING, RecordKind.EPISODE, "e1") == e
    from .postgres_record_support import events

    assert events(stores.records) == ()
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT count(*) FROM memory_event_records").fetchone() == (0,)
    stores.history.delete(BINDING, cid)
    assert port.get(BINDING, RecordKind.EPISODE, "e1") is None


def test_foreign_binding_references_are_rejected_by_database(stores: Stores) -> None:
    from digital_souls_core.memory_record_store import FactWrite, RecordBatch
    from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

    port = PostgresMemoryRecords(stores.database)
    e, f = episode(citation(stores)), fact(citation(stores))
    port.register(BINDING, RecordBatch(episodes=(e,), facts=(FactWrite(f),)), "v1")
    with pytest.raises(CoreError) as error:
        with stores.database.transaction(BINDING) as db:
            db.execute(
                "INSERT INTO "
                "memory_episode_fact_links(binding,id,version,state,created_at,epi"
                "sode,episode_version,fact,fact_version) VALUES "
                "('foreign','l1',1,'active',now(),'e1',1,'f1',1)"
            )
    assert error.value.code == "storage_unavailable"
    assert port.list(BINDING, RecordKind.EPISODE_FACT_LINK) == ()


@pytest.mark.parametrize("revocation", ["delete", "private", "turn_delete"])
def test_revocation_erases_only_affected_registration_digests(
    stores: Stores, revocation: str
) -> None:
    from digital_souls_core.history import ConversationControls, TurnDeletionInput
    from digital_souls_core.memory_record_store import FactWrite, RecordBatch
    from digital_souls_core.memory_records import EpisodeFactLink, RecordState
    from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

    port = PostgresMemoryRecords(stores.database)
    e, e2 = episode(citation(stores)), episode(citation(stores), "e2")
    f = fact(e.citations[0])
    link = EpisodeFactLink(
        link_id="l1",
        version=1,
        binding=BINDING,
        created_at=e.created_at,
        state=RecordState.ACTIVE,
        episode=reference(e2),
        fact=reference(f),
    )
    # Separate registrations exercise dependent results and every Fact version.
    batches = (
        RecordBatch(episodes=(e,)),
        RecordBatch(episodes=(e2,)),
        RecordBatch(facts=(FactWrite(f),)),
        RecordBatch(facts=(FactWrite(replace(f, version=2, citations=e2.citations), 1),)),
        RecordBatch(links=(link,)),
        RecordBatch(semantics=(derived(e, e2),)),
    )
    results = [port.register(BINDING, batch, f"v{i}") for i, batch in enumerate(batches)]
    before = snapshot(stores)["memory_record_registrations"]
    assert len(before) == 6 and all(row[3] is not None for row in before)
    cid = e.citations[0].source.reference.conversation_id
    if revocation == "delete":
        stores.history.delete(BINDING, cid)
    elif revocation == "private":
        stores.history.controls(
            BINDING, cid, ConversationControls(expected_revision=1, private_mode=True)
        )
    else:
        stores.history.delete_turns(
            BINDING, cid, TurnDeletionInput(expected_revision=1, turn_revision=1, scope="selected")
        )
    after = snapshot(stores)["memory_record_registrations"]
    # e2 is unrelated; f2, link and Semantic depend on revoked e / f1.
    for i, (old, new) in enumerate(zip(before, after, strict=True)):
        assert new[:3] == old[:3] and new[4] == old[4]
        assert new[3] == (old[3] if i == 1 else None)
    assert port.register(BINDING, batches[1], "v1") == results[1]
    PostgresDatabase(stores.config).initialize()


@pytest.mark.parametrize("changed", [False, True])
def test_null_registration_digest_rejects_same_key_retry(stores: Stores, changed: bool) -> None:
    from digital_souls_core.memory_record_store import RecordBatch
    from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

    port = PostgresMemoryRecords(stores.database)
    e = episode(citation(stores))
    port.register(BINDING, RecordBatch(episodes=(e,)), "v1")
    stores.history.delete(BINDING, e.citations[0].source.reference.conversation_id)
    before = snapshot(stores)
    retry = (
        replace(e, episode_id="new-id", normalized_text="Changed synthetic body") if changed else e
    )
    with pytest.raises(CoreError) as error:
        port.register(BINDING, RecordBatch(episodes=(retry,)), "v1")
    assert error.value.code == "memory_registration_conflict"
    assert snapshot(stores) == before
    assert port.list(BINDING, RecordKind.EPISODE) == ()


def test_revocation_digest_erasure_rolls_back_with_history_and_records(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch
    from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

    port = PostgresMemoryRecords(stores.database)
    e = episode(citation(stores))
    batch = RecordBatch(episodes=(e,))
    result = port.register(BINDING, batch, "v1")
    before = snapshot(stores)
    original = Connection.execute

    def interrupted(db: Connection[tuple[Any, ...]], query: Any, *args: Any, **kwargs: Any) -> Any:
        text = query.as_string(db) if isinstance(query, sql.Composable) else str(query)
        value = original(db, query, *args, **kwargs)
        if text.startswith("UPDATE memory_record_registrations SET request_digest=NULL"):
            assert db.execute(
                "SELECT request_digest FROM memory_record_registrations"
            ).fetchall() == [(None,)]
            raise RuntimeError("Synthetic digest erasure interruption")
        return value

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", interrupted)
        with pytest.raises(RuntimeError, match="Synthetic digest erasure interruption"):
            stores.history.delete(BINDING, e.citations[0].source.reference.conversation_id)
    assert snapshot(stores) == before
    assert port.register(BINDING, batch, "v1") == result
    stores.history.delete(BINDING, e.citations[0].source.reference.conversation_id)
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT request_digest FROM memory_record_registrations").fetchall() == [
            (None,)
        ]


def test_record_schema_rejects_nonnullable_registration_digest(stores: Stores) -> None:
    with stores.database.transaction(BINDING) as db:
        db.execute(
            "ALTER TABLE memory_record_registrations ALTER COLUMN request_digest SET NOT NULL"
        )
    with pytest.raises(CoreError) as error:
        PostgresDatabase(stores.config).initialize()
    assert error.value.code == "storage_schema"

"""Verbatim erasure must preserve all non-verbatim rows and canonical retrieval."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from psycopg import sql

from digital_souls_core.history import TurnDeletionInput
from digital_souls_core.postgres_db import PostgresDatabase, key
from digital_souls_core.postgres_schema import SCHEMA_VERSION

from . import test_postgres_stores
from .postgres_record_support import register_sources
from .postgres_v5_fixture import V5_DDL
from .record_retrieval_support import setup
from .test_postgres_memory_record_schema import snapshot
from .test_postgres_record_retrieval import records
from .test_postgres_stores import BINDING, MESSAGES, Stores, raw_connection, seed

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores
REMOVED = {"memories", "memory_sources", "memory_jobs"}


def install_v5(stores: Stores) -> None:
    with raw_connection(stores.config) as db:
        db.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(stores.config.schema_name))
        )
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(stores.config.schema_name)))
        db.execute(
            sql.SQL("SET search_path TO {}, pg_temp").format(
                sql.Identifier(stores.config.schema_name)
            )
        )
        for statement in V5_DDL:
            db.execute(statement)


@contextmanager
def writable_v5(stores: Stores) -> Iterator[None]:
    # Seed legacy rows through the current port, then remove its new nullable
    # column before snapshots/migration. The migration source remains frozen v5.
    with stores.database.transaction(BINDING) as db:
        db.execute("ALTER TABLE turns ADD COLUMN card_sha256 TEXT")
    try:
        yield
    finally:
        with stores.database.transaction(BINDING) as db:
            db.execute("ALTER TABLE turns DROP COLUMN card_sha256")


async def test_v5_removal_preserves_rows_and_reopens(stores: Stores) -> None:
    install_v5(stores)
    with writable_v5(stores):
        port, expected = records(stores, stores.records)
        seed_preserved_state(stores)
    before = snapshot(stores)
    PostgresDatabase(stores.config).initialize()
    after = snapshot(stores)
    assert set(after) == set(before) - REMOVED
    assert all(row[-1] is None for row in after["turns"])
    assert [row[:-1] for row in after["turns"]] == before["turns"]
    assert all(after[t] == rows for t, rows in before.items() if t not in REMOVED | {"turns"})
    assert all(
        before[t]
        for t in (
            "conversations",
            "turns",
            "memory_events",
            "memory_event_records",
            "source_deletions",
            "turn_tombstones",
            "turn_deletions",
            "memory_episodes",
            "memory_fact_versions",
            "memory_semantics",
        )
    )
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
    PostgresDatabase(stores.config).initialize()
    assert port.retrievable(BINDING) == expected
    service, _, _ = setup()
    service.records = port
    assert set(await service.search(BINDING, "tea")) == set(expected)


def seed_preserved_state(stores: Stores) -> None:
    # Each kept feature carries nonempty data before migration.
    pending = stores.history.create(BINDING).conversation_id
    stores.history.append(
        BINDING,
        pending,
        "pending",
        "pending-fp",
        0,
        MESSAGES,
        "stop",
        memory_confirmation_indices=(0,),
    )
    deleted = seed(stores)
    register_sources(stores.records, stores.history, (deleted,))
    stores.history.delete(BINDING, deleted.conversation_id)
    turn = seed(stores)
    stores.history.delete_turns(
        BINDING,
        turn.conversation_id,
        TurnDeletionInput(expected_revision=1, turn_revision=1, scope="selected"),
    )
    with stores.database.transaction(BINDING) as db:
        db.execute(
            "INSERT INTO memory_jobs(binding,id,sources,versions,state) "
            "VALUES (%s,'old-job','[]','synthetic-v1','done')",
            (key(BINDING),),
        )
        db.execute(
            "INSERT INTO memories(binding,id,job,kind,body,state) "
            "VALUES (%s,'old-memory','old-job','semantic','Synthetic verbatim body','active')",
            (key(BINDING),),
        )
        db.execute(
            "INSERT INTO memory_sources(binding,memory,conversation,revision,position,epoch) "
            "VALUES (%s,'old-memory',%s,1,0,0)",
            (key(BINDING), pending),
        )


@pytest.mark.parametrize("table", sorted(REMOVED))
def test_current_schema_rejects_leftover_verbatim_table(stores: Stores, table: str) -> None:
    from digital_souls_core.application import CoreError

    with stores.database.transaction(BINDING) as db:
        db.execute("UPDATE schema_version SET version=%s", (SCHEMA_VERSION,))
        db.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS {} (value text)").format(sql.Identifier(table))
        )
    with pytest.raises(CoreError) as error:
        PostgresDatabase(stores.config).initialize()
    assert error.value.code == "storage_schema"


@pytest.mark.parametrize("after", ["sources", "memories", "jobs", "version"])
def test_v5_removal_rolls_back_every_drop_and_retries(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, after: str
) -> None:
    from typing import Any

    from psycopg import Connection

    install_v5(stores)
    with writable_v5(stores):
        seed_preserved_state(stores)
    before = snapshot(stores)
    original = Connection.execute
    target = {
        "sources": "DROP TABLE memory_sources",
        "memories": "DROP TABLE memories",
        "jobs": "DROP TABLE memory_jobs",
        "version": "UPDATE schema_version SET version=6",
    }[after]

    def execute(db: Connection[tuple[Any, ...]], query: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(db, query, *args, **kwargs)
        if str(query) == target:
            raise RuntimeError("Synthetic removal interruption")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", execute)
        with pytest.raises(RuntimeError, match="Synthetic removal interruption"):
            PostgresDatabase(stores.config).initialize()
    assert snapshot(stores) == before
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(5,)]
    PostgresDatabase(stores.config).initialize()
    assert not REMOVED & set(snapshot(stores))


def test_current_schema_has_no_verbatim_relations_and_reopens(stores: Stores) -> None:
    before = snapshot(stores)
    assert not REMOVED & set(before)
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
        assert (
            db.execute(
                "SELECT 1 FROM pg_indexes WHERE schemaname=%s AND indexname='memory_source_lookup'",
                (stores.config.schema_name,),
            ).fetchall()
            == []
        )
    PostgresDatabase(stores.config).initialize()
    assert snapshot(stores) == before


@pytest.mark.parametrize(
    "alteration",
    [
        "ALTER TABLE memories ALTER COLUMN body TYPE varchar",
        "ALTER TABLE memory_sources DROP CONSTRAINT memory_sources_binding_memory_fkey",
        "DROP INDEX memory_source_lookup",
    ],
)
def test_malformed_v5_is_rejected_before_any_verbatim_drop(stores: Stores, alteration: str) -> None:
    from digital_souls_core.application import CoreError

    install_v5(stores)
    with writable_v5(stores):
        seed_preserved_state(stores)
    with stores.database.transaction(BINDING) as db:
        db.execute(alteration)
    before = snapshot(stores)
    with pytest.raises(CoreError) as error:
        PostgresDatabase(stores.config).initialize()
    assert error.value.code == "storage_schema"
    assert snapshot(stores) == before
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(5,)]

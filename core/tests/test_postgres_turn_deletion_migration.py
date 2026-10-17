"""PostgreSQL v3 migration preserves history and rolls back incomplete DDL."""

import json
from typing import Any

import pytest
from psycopg import Connection, sql

from digital_souls_core.history import SourceReference
from digital_souls_core.postgres_db import PostgresDatabase
from digital_souls_core.postgres_history import PostgresHistory
from digital_souls_core.postgres_memory_records import PostgresMemoryRecords
from digital_souls_core.postgres_schema import SCHEMA_VERSION

from . import test_postgres_stores
from .memory_confirmation_contracts import make_harness
from .test_postgres_stated_at import CID, install_v1
from .test_postgres_stores import BINDING, Stores
from .time_support import FIRST
from .turn_deletion_contracts import delete_turns

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


def install_v3(stores: Stores, timestamp: object, state: str) -> None:
    install_v1(stores)
    with stores.database.transaction(BINDING) as db:
        db.execute("ALTER TABLE turns ADD COLUMN stated_at timestamptz")
        db.execute("ALTER TABLE turns ADD COLUMN memory_confirmation TEXT NOT NULL DEFAULT '{}'")
        db.execute("UPDATE turns SET stated_at=%s,memory_confirmation=%s", (timestamp, state))
        db.execute("UPDATE schema_version SET version=3")


@pytest.mark.parametrize("timestamp", [None, FIRST])
@pytest.mark.parametrize("state", ['{"0":null}', '{"0":false}', '{"0":true}'])
def test_v3_migration_preserves_stored_time_confirmation_and_receipt(
    stores: Stores,
    timestamp: object,
    state: str,
) -> None:
    install_v3(stores, timestamp, state)
    with stores.database.transaction(BINDING) as db:
        original = db.execute("SELECT * FROM turns").fetchall()
    database = PostgresDatabase(stores.config)
    h = make_harness(PostgresHistory(database), PostgresMemoryRecords(database))
    with h.http:
        with stores.database.transaction(BINDING) as db:
            migrated = db.execute("SELECT * FROM turns").fetchall()
            assert all(row[-1] is None for row in migrated)
            assert [row[:-1] for row in migrated] == original
            assert db.execute("SELECT version FROM schema_version").fetchall() == [
                (SCHEMA_VERSION,)
            ]
        snapshot = h.conversation.read("synthetic", CID)
        assert snapshot.memory_sources[0].stated_at == timestamp
        assert snapshot.memory_confirmations == (
            (SourceReference(CID, 1, 0),) if json.loads(state)["0"] is None else ()
        )
        assert h.conversation.store.source_eligible(BINDING, SourceReference(CID, 1, 0)) == (
            json.loads(state)["0"] is False
        )
        receipt = h.conversation.store.receipt(BINDING, CID, "r1")
        assert receipt is not None and receipt.fingerprint == "original-fingerprint"
        delete_turns(h, CID, 1, 1, "selected")
        assert h.conversation.read("synthetic", CID).messages == ()


def test_v3_migration_interruption_rolls_back_and_allows_retry(
    stores: Stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_v3(stores, FIRST, '{"0":null}')
    with stores.database.transaction(BINDING) as db:
        original_rows = db.execute("SELECT * FROM turns").fetchall()
        tables = db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename",
            (stores.config.schema_name,),
        ).fetchall()
    original = Connection.execute

    def interrupted(db: Connection[tuple[Any, ...]], query: Any, *args: Any, **kwargs: Any) -> Any:
        text = query.as_string(db) if isinstance(query, sql.Composable) else str(query)
        cursor = original(db, query, *args, **kwargs)
        if text.lstrip().upper().startswith("CREATE TABLE"):
            raise RuntimeError("Synthetic migration interruption")
        return cursor

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", interrupted)
        with pytest.raises(RuntimeError, match="Synthetic migration interruption"):
            PostgresDatabase(stores.config).initialize()
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(3,)]
        assert db.execute("SELECT * FROM turns").fetchall() == original_rows
        assert (
            db.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename",
                (stores.config.schema_name,),
            ).fetchall()
            == tables
        )
    database = PostgresDatabase(stores.config)
    h = make_harness(PostgresHistory(database), PostgresMemoryRecords(database))
    with h.http:
        delete_turns(h, CID, 1, 1, "selected")

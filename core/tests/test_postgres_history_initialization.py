"""Factory, fail-closed schema and process-exit recovery contracts."""

import subprocess
import sys

import pytest
from psycopg import sql

from digital_souls_core.application import CoreError
from digital_souls_core.postgres_db import _lock_key
from digital_souls_core.postgres_history import PostgresHistory
from digital_souls_core.postgres_memory_records import PostgresMemoryRecords
from digital_souls_core.postgres_schema import SCHEMA_VERSION
from digital_souls_core.storage import StorageConfig, open_storage

from . import test_postgres_stores
from .postgres_history_support import history
from .test_postgres_stores import BINDING, MESSAGES, Stores, raw_connection

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


def test_factory_shares_explicit_database_without_extracting(stores: Stores) -> None:
    opened = open_storage(StorageConfig(backend="postgresql", postgres=stores.config))
    created = opened.history.create(BINDING)
    assert isinstance(opened.records, PostgresMemoryRecords)
    assert isinstance(opened.history, PostgresHistory)
    assert opened.records.database is opened.history.database
    opened.history.append(BINDING, created.conversation_id, "r1", "fp1", 0, MESSAGES, "stop")
    assert opened.history.read(BINDING, created.conversation_id).messages == MESSAGES
    assert opened.records.retrievable(BINDING) == ()


def test_unknown_unversioned_schema_is_preserved_and_rejected(stores: Stores) -> None:
    with raw_connection(stores.config) as db:
        db.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(stores.config.schema_name))
        )
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(stores.config.schema_name)))
    with stores.database.transaction(BINDING) as db:
        db.execute("CREATE TABLE unknown (value TEXT)")
        db.execute("INSERT INTO unknown VALUES ('Synthetic preserved value')")
    with pytest.raises(CoreError) as error:
        history(stores)
    assert error.value.code == "storage_schema"
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT value FROM unknown").fetchall() == [
            ("Synthetic preserved value",)
        ]
        assert db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s",
            (stores.config.schema_name,),
        ).fetchall() == [("unknown",)]


@pytest.mark.parametrize("crash_at", ["create table turns", "insert into schema_version"])
def test_first_schema_creation_recovers_after_process_exit(stores: Stores, crash_at: str) -> None:
    with raw_connection(stores.config) as db:
        db.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(stores.config.schema_name))
        )
    script = """
import os, sys
from psycopg import Connection, sql
from digital_souls_core.postgres_db import PostgresConfig, PostgresDatabase
from digital_souls_core.postgres_history import PostgresHistory
original = Connection.execute

def execute(db, query, *args, **kwargs):
    cursor = original(db, query, *args, **kwargs)
    text = query.as_string(db) if isinstance(query, sql.Composable) else str(query)
    if ' '.join(text.lower().split()).startswith(sys.argv[2]):
        os._exit(73)
    return cursor

Connection.execute = execute
PostgresHistory(PostgresDatabase(PostgresConfig.model_validate_json(sys.argv[1])))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, stores.config.model_dump_json(), crash_at],
        check=False,
        timeout=15,
    )
    assert result.returncode == 73
    # Wait for the interrupted transaction's schema lock before observing rollback.
    with stores.database._connect() as db:
        db.execute(
            "SELECT pg_advisory_xact_lock(%s)", (_lock_key("schema", stores.config.schema_name),)
        )
        assert (
            db.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname=%s", (stores.config.schema_name,)
            ).fetchall()
            == []
        )
    restored = history(stores)
    assert restored.create(BINDING).revision == 0
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
        assert {
            row[0]
            for row in db.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=%s", (stores.config.schema_name,)
            )
        } == set(stores.database._tables(SCHEMA_VERSION))


def test_deleted_conversation_cannot_be_resurrected_by_late_append(stores: Stores) -> None:
    store = history(stores)
    cid = store.create(BINDING).conversation_id
    store.append(BINDING, cid, "r1", "fp1", 0, MESSAGES, "stop")
    store.delete(BINDING, cid)
    with pytest.raises(CoreError) as error:
        history(stores).append(BINDING, cid, "r2", "fp2", 1, MESSAGES, "stop")
    assert error.value.code == "revision_conflict"
    assert store.list(BINDING) == []
    assert store.receipt(BINDING, cid, "r2") is None

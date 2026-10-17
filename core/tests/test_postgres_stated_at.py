"""Synthetic PostgreSQL contracts for turn timestamps and the v1 migration."""

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from psycopg import Connection, sql

from digital_souls_core.application import CoreError
from digital_souls_core.contracts import Message
from digital_souls_core.history import ConversationControls
from digital_souls_core.postgres_db import PostgresDatabase, key
from digital_souls_core.postgres_history import PostgresHistory
from digital_souls_core.postgres_schema import SCHEMA_VERSION

from . import test_postgres_stores
from .postgres_v1_fixture import V1_DDL
from .test_postgres_stores import BINDING, MESSAGES, Stores, raw_connection
from .time_support import FIRST, SECOND

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores
CID = "synthetic-legacy"


def install_v1(stores: Stores) -> None:
    # This fixture owns exactly the random schema created by stores, never an
    # operator database. DDL is frozen so new production DDL cannot fake v1.
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
        for statement in V1_DDL:
            db.execute(statement)
        db.execute(
            "INSERT INTO conversations (binding,id,revision) VALUES (%s,%s,1)", (key(BINDING), CID)
        )
        db.execute(
            "INSERT INTO turns (binding,conversation,request,fingerprint,revision,messages,finish) "
            "VALUES (%s,%s,'r1','original-fingerprint',1,%s,'stop')",
            (key(BINDING), CID, json.dumps([m.model_dump(exclude_none=True) for m in MESSAGES])),
        )


def test_fixed_clock_reopen_preserves_source_dates(stores: Stores) -> None:
    times = iter((FIRST.astimezone(timezone(timedelta(hours=-5))), SECOND))
    history = PostgresHistory(stores.database, clock=lambda: next(times))
    cid = history.create(BINDING).conversation_id
    messages = (
        MESSAGES[0],
        Message(role="tool", content="Synthetic result", tool_call_id="c"),
        MESSAGES[-1],
    )
    receipt = history.append(BINDING, cid, "r1", "fp1", 0, messages, "stop")
    history.controls(BINDING, cid, ConversationControls(expected_revision=1, archived=True))
    history.append(BINDING, cid, "r2", "fp2", 2, MESSAGES, "stop")
    assert history.append(BINDING, cid, "r1", "fp1", 0, messages, "stop") == receipt
    memory = PostgresHistory(PostgresDatabase(stores.config), clock=lambda: SECOND)
    snapshot = memory.read(BINDING, cid)
    assert snapshot.messages == messages + MESSAGES
    assert [s.reference.turn_revision for s in snapshot.memory_sources] == [1, 1, 1, 3, 3]
    assert [s.stated_at for s in snapshot.memory_sources] == [FIRST] * 3 + [SECOND] * 2
    assert all(
        s.stated_at is not None and s.stated_at.utcoffset() == timedelta(0)
        for s in snapshot.memory_sources
    )


def test_naive_clock_rolls_back_and_valid_retry_saves_timestamp(stores: Stores) -> None:
    times = iter((FIRST.replace(tzinfo=None), FIRST))
    history = PostgresHistory(stores.database, clock=lambda: next(times))
    created = history.create(BINDING)
    with pytest.raises((ValueError, CoreError)):
        history.append(BINDING, created.conversation_id, "r1", "fp", 0, MESSAGES, "stop")
    assert history.read(BINDING, created.conversation_id) == created
    assert history.receipt(BINDING, created.conversation_id, "r1") is None
    history.append(BINDING, created.conversation_id, "r1", "fp", 0, MESSAGES, "stop")
    assert history.read(BINDING, created.conversation_id).memory_sources[0].stated_at == FIRST


def test_default_clock_is_current_utc(stores: Stores) -> None:
    cid = stores.history.create(BINDING).conversation_id
    before = datetime.now(UTC)
    stores.history.append(BINDING, cid, "r1", "fp", 0, MESSAGES, "stop")
    after = datetime.now(UTC)
    timestamp = (
        PostgresHistory(PostgresDatabase(stores.config))
        .read(BINDING, cid)
        .memory_sources[0]
        .stated_at
    )
    assert timestamp is not None and before <= timestamp <= after
    assert timestamp.utcoffset() == timedelta(0)


def test_v1_migration_preserves_old_null_history_and_receipt(stores: Stores) -> None:
    install_v1(stores)
    memory = PostgresHistory(PostgresDatabase(stores.config))
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
        assert db.execute("SELECT stated_at FROM turns").fetchall() == [(None,)]
    snapshot = memory.read(BINDING, CID)
    assert snapshot.messages == MESSAGES and snapshot.revision == 1
    assert [s.stated_at for s in snapshot.memory_sources] == [None, None]
    receipt = memory.receipt(BINDING, CID, "r1")
    assert receipt is not None and receipt.fingerprint == "original-fingerprint"
    assert memory.append(BINDING, CID, "r1", "original-fingerprint", 0, MESSAGES, "stop") == receipt
    assert memory.read(BINDING, CID) == snapshot
    current = PostgresHistory(PostgresDatabase(stores.config), clock=lambda: FIRST)
    current.append(BINDING, CID, "r2", "fp2", 1, MESSAGES, "stop")
    assert [s.stated_at for s in current.read(BINDING, CID).memory_sources] == [
        None,
        None,
        FIRST,
        FIRST,
    ]
    assert current.receipt(BINDING, CID, "r1") == receipt


@pytest.mark.parametrize("after", ["column", "version"])
def test_v1_migration_interruption_rolls_back_and_retries(
    stores: Stores,
    monkeypatch: pytest.MonkeyPatch,
    after: str,
) -> None:
    install_v1(stores)
    original = Connection.execute
    interrupted = False

    def execute(db: Connection[tuple[Any, ...]], query: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal interrupted
        text = query.as_string(db) if isinstance(query, sql.Composable) else str(query)
        cursor = original(db, query, *args, **kwargs)
        normalized = " ".join(text.lower().replace('"', "").split())
        if (
            after == "column"
            and normalized.startswith("alter table turns")
            and "stated_at" in normalized
            or after == "version"
            and normalized.startswith("update schema_version")
        ):
            interrupted = True
            raise RuntimeError("Synthetic migration interruption")
        return cursor

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", execute)
        with pytest.raises(RuntimeError, match="Synthetic migration interruption"):
            PostgresDatabase(stores.config).initialize()
    assert interrupted
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(1,)]
        assert (
            db.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='turns' AND column_name='stated_at'",
                (stores.config.schema_name,),
            ).fetchall()
            == []
        )
        assert db.execute("SELECT fingerprint,messages FROM turns").fetchall() == [
            (
                "original-fingerprint",
                json.dumps([m.model_dump(exclude_none=True) for m in MESSAGES]),
            )
        ]
    restored = PostgresHistory(PostgresDatabase(stores.config))
    assert restored.read(BINDING, CID).messages == MESSAGES
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
        assert db.execute("SELECT stated_at FROM turns").fetchall() == [(None,)]


@pytest.mark.parametrize(
    "alteration",
    [
        "ALTER TABLE turns ALTER COLUMN stated_at TYPE timestamp without time zone",
        "ALTER TABLE turns ALTER COLUMN stated_at SET NOT NULL",
        "ALTER TABLE turns ALTER COLUMN stated_at SET DEFAULT CURRENT_TIMESTAMP",
    ],
)
def test_timestamp_schema_drift_is_rejected(stores: Stores, alteration: str) -> None:
    with stores.database.transaction(BINDING) as db:
        db.execute(alteration)
    with pytest.raises(CoreError):
        PostgresDatabase(stores.config).initialize()
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]


def test_malformed_v1_is_rejected_before_migration(stores: Stores) -> None:
    install_v1(stores)
    with stores.database.transaction(BINDING) as db:
        db.execute("ALTER TABLE conversations ALTER COLUMN private_mode SET DEFAULT true")
    with pytest.raises(CoreError):
        PostgresDatabase(stores.config).initialize()
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(1,)]
        assert (
            db.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='turns' AND column_name='stated_at'",
                (stores.config.schema_name,),
            ).fetchall()
            == []
        )

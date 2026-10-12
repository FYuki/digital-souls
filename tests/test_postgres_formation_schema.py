"""Version 7 -> 8 atomic migration and exact ledger physical contract."""

from typing import Any

import pytest
from psycopg import Connection

from digital_souls_core.application import CoreError
from digital_souls_core.postgres_db import PostgresDatabase
from digital_souls_core.postgres_schema import SCHEMA_VERSION

from . import test_postgres_stores
from .test_postgres_card_hash import install_v6
from .test_postgres_stores import BINDING, Stores

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


def install_v7(stores: Stores) -> str:
    cid = install_v6(stores)
    with stores.database.transaction(BINDING) as db:
        db.execute("ALTER TABLE turns ADD COLUMN card_sha256 TEXT")
        db.execute("UPDATE turns SET card_sha256=%s", ("a" * 64,))
        db.execute("UPDATE schema_version SET version=7")
    return cid


def test_v7_migration_preserves_receipt_and_does_not_import_old_history(stores: Stores) -> None:
    cid = install_v7(stores)
    with stores.database.transaction(BINDING) as db:
        before = db.execute("SELECT * FROM turns").fetchall()
    PostgresDatabase(stores.config).initialize()
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT * FROM turns").fetchall() == before
        assert db.execute("SELECT * FROM formation_jobs").fetchall() == []
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
    PostgresDatabase(stores.config).initialize()
    receipt = stores.history.receipt(BINDING, cid, "r1")
    assert receipt is not None and receipt.card_sha256 == "a" * 64


@pytest.mark.parametrize("after", ["table", "version"])
def test_v7_migration_rollback_and_retry(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, after: str
) -> None:
    install_v7(stores)
    original = Connection.execute

    def interrupted(db: Connection[tuple[Any, ...]], query: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(db, query, *args, **kwargs)
        if (after == "table" and str(query).startswith("CREATE TABLE formation_jobs")) or (
            after == "version" and str(query) == "UPDATE schema_version SET version=8"
        ):
            raise RuntimeError("Synthetic formation migration interruption")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", interrupted)
        with pytest.raises(RuntimeError, match="Synthetic formation migration"):
            PostgresDatabase(stores.config).initialize()
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(7,)]
        assert db.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema=%s "
            "AND table_name='formation_jobs'",
            (stores.config.schema_name,),
        ).fetchone() == (0,)
    PostgresDatabase(stores.config).initialize()
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]


@pytest.mark.parametrize(
    "alteration",
    [
        "ALTER TABLE formation_jobs DROP CONSTRAINT formation_jobs_source_fkey",
        "ALTER TABLE formation_jobs DROP CONSTRAINT formation_jobs_pkey",
        "ALTER TABLE formation_jobs DROP CONSTRAINT formation_jobs_lease_check",
        "ALTER TABLE formation_jobs DROP CONSTRAINT formation_jobs_state_check",
        "ALTER TABLE formation_jobs ALTER COLUMN target_revision TYPE bigint",
        "ALTER TABLE formation_jobs ALTER COLUMN attempts SET DEFAULT 0",
        "ALTER TABLE formation_jobs ALTER COLUMN lease_until SET NOT NULL",
        "ALTER TABLE formation_jobs ADD COLUMN body text",
    ],
)
def test_malformed_ledger_schema_rejected(stores: Stores, alteration: str) -> None:
    with stores.database.transaction(BINDING) as db:
        db.execute(alteration)
    with pytest.raises(CoreError) as error:
        PostgresDatabase(stores.config).initialize()
    assert error.value.code == "storage_schema"

"""Read only this case's PostgreSQL schema; never inspect server files/WAL."""

from collections.abc import Callable
from datetime import datetime

from psycopg import sql

from digital_souls_core.postgres_db import PostgresDatabase
from digital_souls_core.postgres_history import PostgresHistory

from .test_postgres_stores import BINDING, Stores


def history(stores: Stores, *, clock: Callable[[], datetime] | None = None) -> PostgresHistory:
    database = PostgresDatabase(stores.config)
    return PostgresHistory(database) if clock is None else PostgresHistory(database, clock=clock)


def assert_text_absent(stores: Stores, text: str) -> None:
    with stores.database.transaction(BINDING) as db:
        tables = db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s",
            (stores.config.schema_name,),
        ).fetchall()
        assert tables
        for (table,) in tables:
            rows = db.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))).fetchall()
            assert text not in str(rows), f"Unexpected synthetic text in {table}"

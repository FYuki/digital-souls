"""Measure real PostgreSQL statements and fetched source rows for batched reads."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from psycopg import Connection, sql


@dataclass
class Queries:
    statements: list[str] = field(default_factory=list)
    source_bodies: list[str] = field(default_factory=list)


@contextmanager
def observe_queries(monkeypatch: pytest.MonkeyPatch) -> Iterator[Queries]:
    observed = Queries()
    original = Connection.execute

    def execute(
        connection: Connection[tuple[Any, ...]], query: Any, *args: Any, **kwargs: Any
    ) -> Any:
        text = query.as_string(connection) if isinstance(query, sql.Composable) else str(query)
        observed.statements.append(text)
        cursor = original(connection, query, *args, **kwargs)
        # Inspect libpq's result without consuming the cursor or issuing another
        # query. This measures bodies transferred from PostgreSQL, not Python
        # helper invocations or just the final filtered result.
        result = cursor.pgresult
        if result is not None:
            for column_index, column in enumerate(cursor.description or ()):
                if column.name == "messages":
                    for row in range(result.ntuples):
                        value = result.get_value(row, column_index)
                        assert value is not None
                        observed.source_bodies.append(value.decode("utf-8"))
        return cursor

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", execute)
        yield observed

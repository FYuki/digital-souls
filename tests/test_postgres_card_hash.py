"""Persist response Card identity, preserving unknown legacy values and receipts."""

import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg import Connection

from digital_souls_core.api import create_app
from digital_souls_core.application import CoreError, Inference
from digital_souls_core.conversations import request_fingerprint
from digital_souls_core.postgres_db import PostgresDatabase, key
from digital_souls_core.postgres_schema import SCHEMA_VERSION

from . import test_postgres_stores
from .conversation_support import SyntheticPolicy, turn
from .postgres_history_support import history
from .support import FakeProvider, character
from .test_postgres_stores import BINDING, MESSAGES, Stores
from .test_postgres_verbatim_removal import install_v5

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


@pytest.mark.parametrize("stream", [False, True])
def test_hash_saved_restored_listed_and_retried_after_card_change(
    stores: Stores, stream: bool
) -> None:
    char = character("synthetic")
    provider = FakeProvider()
    store = history(stores)
    base = "/v1/characters/synthetic/conversations"
    body = turn(stream=stream).model_dump()
    with TestClient(
        create_app(
            Inference((char,), provider), history_store=store, history_policy=SyntheticPolicy()
        ),
        base_url="http://127.0.0.1",
    ) as http:
        cid = http.post(base).json()["conversation_id"]
        url = f"{base}/{cid}"
        result = http.post(url + "/completions", json=body)
        assert result.status_code == 200
        assert result.headers["X-Character-Card-SHA256"] == char.card_sha256
        assert http.get(base).json()["conversation_ids"] == [cid]
        assert http.get(url).json()["turns"] == [
            {"turn_revision": 1, "card_sha256": char.card_sha256}
        ]
    reopened = history(stores)
    receipt = reopened.receipt(BINDING, cid, "r1")
    assert receipt is not None and receipt.card_sha256 == char.card_sha256
    assert reopened.read(BINDING, cid).turns[0].card_sha256 == char.card_sha256
    changed = replace(char, card_sha256="b" * 64)
    with TestClient(
        create_app(
            Inference((changed,), provider),
            history_store=reopened,
            history_policy=SyntheticPolicy(),
        ),
        base_url="http://127.0.0.1",
    ) as http:
        retry = http.post(url + "/completions", json=body)
        assert retry.status_code == 200 and retry.text == result.text
        assert retry.headers["X-Character-Card-SHA256"] == char.card_sha256
        assert len(provider.calls) == 1
        next_body = turn(request_id="r2", expected_revision=1, stream=stream).model_dump()
        response = http.post(url + "/completions", json=next_body)
        assert response.status_code == 200
        assert response.headers["X-Character-Card-SHA256"] == changed.card_sha256
        assert http.get(url).json()["turns"] == [
            {"turn_revision": 1, "card_sha256": char.card_sha256},
            {"turn_revision": 2, "card_sha256": changed.card_sha256},
        ]
        # Metadata stays outside the provider messages, and deletion removes it.
        assert all(
            "card_sha256" not in message
            for _, payload in provider.calls
            for message in payload["messages"]
        )
        deletion = http.post(
            url + "/turn-deletions",
            json={"expected_revision": 2, "turn_revision": 1, "scope": "selected"},
        )
        assert deletion.status_code == 200
        assert http.get(url).json()["turns"] == [
            {"turn_revision": 2, "card_sha256": changed.card_sha256}
        ]
    assert len(provider.calls) == 2


def install_v6(stores: Stores, *, stream: bool = False) -> str:
    install_v5(stores)
    with stores.database.transaction(BINDING) as db:
        db.execute("DROP TABLE memory_sources")
        db.execute("DROP TABLE memories")
        db.execute("DROP TABLE memory_jobs")
        db.execute("UPDATE schema_version SET version=6")
        cid = "synthetic-v6"
        db.execute(
            "INSERT INTO conversations(binding,id,revision) VALUES (%s,%s,1)", (key(BINDING), cid)
        )
        db.execute(
            "INSERT INTO turns(binding,conversation,request,fingerprint,revision,messages,finish) "
            "VALUES (%s,%s,'r1',%s,1,%s,'stop')",
            (
                key(BINDING),
                cid,
                request_fingerprint(turn(stream=stream), character("synthetic").config),
                json.dumps([m.model_dump(exclude_none=True) for m in MESSAGES]),
            ),
        )
    return cid


@pytest.mark.parametrize("stream", [False, True])
def test_v6_migration_keeps_unknown_hash_and_retry_header_absent(
    stores: Stores, stream: bool
) -> None:
    cid = install_v6(stores, stream=stream)
    store = history(stores)
    PostgresDatabase(stores.config).initialize()
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
        assert db.execute("SELECT card_sha256 FROM turns").fetchall() == [(None,)]
    assert store.read(BINDING, cid).turns[0].card_sha256 is None
    provider = FakeProvider()
    with TestClient(
        create_app(
            Inference((character("synthetic"),), provider),
            history_store=store,
            history_policy=SyntheticPolicy(),
        ),
        base_url="http://127.0.0.1",
    ) as http:
        url = f"/v1/characters/synthetic/conversations/{cid}"
        assert http.get(url).json()["turns"] == [{"turn_revision": 1, "card_sha256": None}]
        result = http.post(url + "/completions", json=turn(stream=stream).model_dump())
        assert result.status_code == 200
        assert "X-Character-Card-SHA256" not in result.headers
    assert provider.calls == []


@pytest.mark.parametrize("after", ["column", "version"])
def test_v6_migration_rolls_back_and_retries(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, after: str
) -> None:
    cid = install_v6(stores)
    original = Connection.execute
    target = (
        "ALTER TABLE turns ADD COLUMN card_sha256 TEXT"
        if after == "column"
        else "UPDATE schema_version SET version=7"
    )

    def execute(db: Connection[tuple[Any, ...]], query: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(db, query, *args, **kwargs)
        if str(query) == target:
            raise RuntimeError("Synthetic migration interruption")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", execute)
        with pytest.raises(RuntimeError, match="Synthetic migration interruption"):
            PostgresDatabase(stores.config).initialize()
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(6,)]
        assert (
            db.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='turns' AND column_name='card_sha256'",
                (stores.config.schema_name,),
            ).fetchall()
            == []
        )
    assert history(stores).read(BINDING, cid).turns[0].card_sha256 is None


@pytest.mark.parametrize(
    "alteration",
    [
        "ALTER COLUMN card_sha256 SET NOT NULL",
        "ALTER COLUMN card_sha256 TYPE varchar",
        "ALTER COLUMN card_sha256 SET DEFAULT 'unknown'",
    ],
)
def test_malformed_current_card_column_rejected(stores: Stores, alteration: str) -> None:
    with stores.database.transaction(BINDING) as db:
        db.execute("ALTER TABLE turns " + alteration)
    with pytest.raises(CoreError) as error:
        PostgresDatabase(stores.config).initialize()
    assert error.value.code == "storage_schema"

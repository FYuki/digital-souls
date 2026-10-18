"""HTTP timestamp contracts use PostgreSQL, including migrated unknown dates."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from digital_souls_core.api import create_app
from digital_souls_core.application import Inference
from digital_souls_core.conversations import Conversations, request_fingerprint

from . import test_postgres_stores
from .conversation_support import SyntheticPolicy, turn
from .postgres_history_support import history
from .support import CALL, TOOL, FakeProvider, character, chunk, completion
from .test_postgres_stated_at import CID, install_v1
from .test_postgres_stores import BINDING, Stores
from .time_support import FIRST, SECOND

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


@pytest.mark.parametrize("stream", [False, True])
def test_http_completion_get_and_patch_preserve_turn_times(stores: Stores, stream: bool) -> None:
    times = iter((FIRST, SECOND))
    store = history(stores, clock=lambda: next(times))
    provider = FakeProvider()
    inference = Inference((character("synthetic"),), provider)
    with TestClient(
        create_app(inference, history_store=store, history_policy=SyntheticPolicy()),
        base_url="http://127.0.0.1",
    ) as http:
        base = "/v1/characters/synthetic/conversations"
        cid = http.post(base).json()["conversation_id"]
        url = f"{base}/{cid}"
        provider.response = completion(tool=True)
        provider.chunks = [chunk({"tool_calls": [{"index": 0, **CALL}]}), chunk({}, "tool_calls")]
        assert (
            http.post(
                url + "/completions", json=turn(tools=[TOOL], stream=stream).model_dump()
            ).status_code
            == 200
        )
        provider.response = completion()
        provider.chunks = [chunk({"content": "Synthetic response"}), chunk({}, "stop")]
        body = turn(
            request_id="r2",
            expected_revision=1,
            tools=[TOOL],
            stream=stream,
            messages=[
                {"role": "tool", "content": "Synthetic result", "tool_call_id": CALL["id"]},
                {"role": "user", "content": "Synthetic next"},
            ],
        ).model_dump()
        response = http.post(url + "/completions", json=body)
        assert response.status_code == 200
        assert http.post(url + "/completions", json=body).text == response.text
        for snapshot in (
            http.get(url),
            http.patch(url, json={"expected_revision": 2, "archived": True}),
        ):
            assert snapshot.status_code == 200
            data = snapshot.json()
            assert [s["turn_revision"] for s in data["memory_sources"]] == [1, 1, 2, 2, 2]
            assert [datetime.fromisoformat(s["stated_at"]) for s in data["memory_sources"]] == [
                FIRST
            ] * 2 + [SECOND] * 3
            assert all("stated_at" not in message for message in data["messages"])
    assert len(provider.calls) == 2


async def test_legacy_http_null_and_service_retry_do_not_infer_again(stores: Stores) -> None:
    config = character("synthetic")
    install_v1(stores)
    with stores.database.transaction(BINDING) as db:
        db.execute("UPDATE turns SET fingerprint=%s", (request_fingerprint(turn(), config.config),))
    store = history(stores)
    provider = FakeProvider()
    service = Conversations(Inference((config,), provider), store, SyntheticPolicy())
    receipt = store.receipt(BINDING, CID, "r1")
    assert await service.complete("synthetic", CID, turn()) == receipt
    assert provider.calls == []
    with TestClient(
        create_app(service.inference, history_store=store, history_policy=service.policy),
        base_url="http://127.0.0.1",
    ) as http:
        response = http.get(f"/v1/characters/synthetic/conversations/{CID}")
        assert response.status_code == 200
        assert [s["stated_at"] for s in response.json()["memory_sources"]] == [None, None]

"""Normalized text storage and production privacy checks against synthetic PostgreSQL."""

import pytest
from fastapi.testclient import TestClient

from digital_souls_core.api import create_app

from . import test_postgres_stores
from .content_parts_support import parts
from .conversation_support import turn
from .postgres_history_support import assert_text_absent, history
from .privacy_support import BINDING
from .support import CALL, TOOL, completion
from .test_postgres_history_privacy import policy_setup
from .test_postgres_stores import Stores

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


@pytest.mark.parametrize("stream", [False, True])
def test_http_text_parts_persist_reopen_and_tool_retry(stores: Stores, stream: bool) -> None:
    service, provider, _, _ = policy_setup(stores)
    with TestClient(
        create_app(service.inference, history_store=service.store, history_policy=service.policy),
        base_url="http://127.0.0.1",
    ) as http:
        base = "/v1/characters/synthetic/conversations"
        cid = http.post(base).json()["conversation_id"]
        url = f"{base}/{cid}"
        first = turn(stream=stream).model_dump()
        first["messages"] = [{"role": "user", "content": parts("one", "two")}]
        response = http.post(url + "/completions", json=first)
        assert response.status_code == 200
        first["messages"] = [{"role": "user", "content": "one\ntwo"}]
        assert http.post(url + "/completions", json=first).text == response.text
        assert history(stores).read(BINDING, cid).messages[0].content == "one\ntwo"
        assert http.get(url).json()["messages"][0]["content"] == "one\ntwo"
        assert len(provider.calls) == 1
        assert provider.calls[0][1]["messages"][-1]["content"] == "one\ntwo"
        provider.response = completion(tool=True)
        assert (
            http.post(
                url + "/completions",
                json=turn(request_id="r2", expected_revision=1, tools=[TOOL]).model_dump(),
            ).status_code
            == 200
        )
        provider.response = completion()
        third = turn(request_id="r3", expected_revision=2, tools=[TOOL], stream=stream).model_dump()
        third["messages"] = [
            {"role": "tool", "tool_call_id": CALL["id"], "content": parts("result", "next")},
            {"role": "user", "content": parts("continue")},
        ]
        assert http.post(url + "/completions", json=third).status_code == 200
        restored = history(stores).read(BINDING, cid)
        assert restored.messages[4].content == "result\nnext"
        assert restored.messages[5].content == "continue"
        assert provider.calls[-1][1]["messages"][-2]["content"] == "result\nnext"


@pytest.mark.parametrize("instruction", ["覚えないで", "保存しないで", "履歴に残さないで"])
@pytest.mark.parametrize("as_parts", [False, True])
async def test_refusal_screening_has_string_parity(
    stores: Stores, instruction: str, as_parts: bool
) -> None:
    service, _, _, _ = policy_setup(stores)
    cid = service.create("synthetic").conversation_id
    text = "Synthetic\n" + instruction
    body = turn(
        messages=[
            {"role": "user", "content": parts("Synthetic", instruction) if as_parts else text}
        ]
    )
    receipt = await service.complete("synthetic", cid, body)
    restored = history(stores).read(BINDING, cid)
    assert restored.messages[0].content == text
    assert receipt.memory_confirmation_indices == (0,)
    assert not restored.memory_sources[0].eligible
    assert len(restored.memory_confirmations) == 1


@pytest.mark.parametrize("as_parts", [False, True])
def test_http_secret_text_parts_denied_before_provider_or_storage(
    stores: Stores, as_parts: bool
) -> None:
    service, provider, classifier, _ = policy_setup(stores, local=False)
    with TestClient(
        create_app(service.inference, history_store=service.store, history_policy=service.policy),
        base_url="http://127.0.0.1",
    ) as http:
        base = "/v1/characters/synthetic/conversations"
        cid = http.post(base).json()["conversation_id"]
        body = turn().model_dump()
        secret = "password: SYNTHETIC_ONLY"
        body["messages"] = [
            {
                "role": "user",
                "content": parts("Synthetic", secret) if as_parts else "Synthetic\n" + secret,
            }
        ]
        response = http.post(f"{base}/{cid}/completions", json=body)
        assert response.status_code == 403
        assert "SYNTHETIC_ONLY" not in response.text
    assert provider.calls == classifier.calls == []
    assert history(stores).read(BINDING, cid).messages == ()
    assert service.store.receipt(BINDING, cid, "r1") is None
    assert_text_absent(stores, "SYNTHETIC_ONLY")

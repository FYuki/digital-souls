from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from digital_souls_core.api import create_app
from digital_souls_core.application import Inference
from digital_souls_core.contracts import Message
from digital_souls_core.history import Binding, HistoryStore, Receipt, Snapshot

from .content_parts_support import INVALID_CONTENT, parts
from .conversation_support import SyntheticPolicy
from .support import CALL, FakeProvider, character
from .test_api import client

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("endpoint", ["chat", "character"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("array_roles", [False, True])
def test_openclaw_tool_roundtrip_normalizes_provider_input(
    endpoint: str, stream: bool, array_roles: bool
) -> None:
    provider = FakeProvider()
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Synthetic user"},
        {"role": "assistant", "content": None, "tool_calls": [CALL]},
        {"role": "tool", "content": "Synthetic result", "tool_call_id": CALL["id"]},
        {"role": "user", "content": parts("Synthetic", "next")},
    ]
    if array_roles:
        for index in (0, 2):
            messages[index]["content"] = parts(messages[index]["content"])
    tools = [
        {"type": "function", "function": {"name": name, "parameters": {}}}
        for name in ["weather", *(f"synthetic_{i}" for i in range(11))]
    ]
    selector = {"model": "miori-alias"} if endpoint == "chat" else {"character_id": "miori"}
    response = client(provider).post(
        f"/v1/{endpoint}/completions",
        json={
            **selector,
            "messages": messages,
            "stream": stream,
            "tools": tools,
            "tool_choice": "auto",
            "max_completion_tokens": 256,
        },
    )
    assert response.status_code == 200
    sent = provider.calls[0][1]
    assert [m.get("content") for m in sent["messages"][1:]] == [
        "Synthetic user",
        None,
        "Synthetic result",
        "Synthetic\nnext",
    ]
    assert sent["messages"][2]["tool_calls"] == [CALL]
    assert sent["tools"] == tools and sent["max_completion_tokens"] == 256
    if stream:
        assert '"content": "こんにちは"' in response.text
        assert response.text.endswith("data: [DONE]\n\n") and provider.closed
    else:
        assert response.json()["choices"][0]["message"]["content"] == "こんにちは"


@pytest.mark.parametrize("endpoint", ["chat", "character", "history"])
@pytest.mark.parametrize("content", INVALID_CONTENT)
def test_http_rejects_invalid_content(endpoint: str, content: Any) -> None:
    provider = FakeProvider()
    store = Mock(spec=HistoryStore)
    http = TestClient(
        create_app(
            Inference((character("synthetic"),), provider),
            history_store=store,
            history_policy=SyntheticPolicy(),
        ),
        base_url="http://127.0.0.1",
    )
    selector: dict[str, Any]
    if endpoint == "history":
        selector = {"request_id": "r1", "expected_revision": 0}
        url = "/v1/characters/synthetic/conversations/synthetic/completions"
    else:
        selector = (
            {"model": "synthetic-alias"} if endpoint == "chat" else {"character_id": "synthetic"}
        )
        url = f"/v1/{endpoint}/completions"
    response = http.post(url, json={**selector, "messages": [{"role": "user", "content": content}]})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert provider.calls == [] and store.mock_calls == []


@pytest.mark.parametrize("stream", [False, True])
def test_history_saves_restores_and_retries_normalized_text(stream: bool) -> None:
    provider = FakeProvider()
    store = Mock(spec=HistoryStore)
    snapshot = Snapshot("synthetic", 0, ())
    receipt: Receipt | None = None
    store.read.side_effect = lambda binding, cid: snapshot
    store.receipt.side_effect = lambda binding, cid, request: receipt

    def append(
        binding: Binding,
        cid: str,
        request: str,
        fingerprint: str,
        revision: int,
        messages: tuple[Message, ...],
        finish: str,
        *,
        card_sha256: str | None = None,
    ) -> Receipt:
        nonlocal snapshot, receipt
        snapshot = Snapshot(cid, revision + 1, (*snapshot.messages, *messages))
        receipt = Receipt(
            fingerprint, snapshot.revision, messages[-1], finish, card_sha256=card_sha256
        )
        return receipt

    store.append.side_effect = append
    with TestClient(
        create_app(
            Inference((character("synthetic"),), provider),
            history_store=store,
            history_policy=SyntheticPolicy(),
        ),
        base_url="http://127.0.0.1",
    ) as http:
        url = "/v1/characters/synthetic/conversations/synthetic"
        body = {
            "request_id": "r1",
            "expected_revision": 0,
            "stream": stream,
            "messages": [{"role": "user", "content": parts("one", "two")}],
        }
        response = http.post(url + "/completions", json=body)
        assert response.status_code == 200
        restored = http.get(url).json()
        assert restored["messages"][0]["content"] == "one\ntwo"
        body["messages"] = [{"role": "user", "content": "one\ntwo"}]
        assert http.post(url + "/completions", json=body).text == response.text
        body["messages"] = [{"role": "user", "content": parts("changed")}]
        assert http.post(url + "/completions", json=body).status_code == 409
    assert provider.calls[0][1]["messages"][-1]["content"] == "one\ntwo"
    assert len(provider.calls) == 1
    store.append.assert_called_once()

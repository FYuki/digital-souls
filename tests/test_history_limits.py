import json
from typing import Any, cast
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from digital_souls_core import conversations
from digital_souls_core.api import create_app
from digital_souls_core.application import CoreError, Inference
from digital_souls_core.contracts import Message
from digital_souls_core.conversations import Conversations
from digital_souls_core.history import Binding, HistoryStore, Receipt, Snapshot

from .conversation_support import SyntheticPolicy, turn
from .support import FakeProvider, character, chunk

pytestmark = pytest.mark.it1


def test_history_api_accepts_combined_input_above_previous_limit() -> None:
    provider = FakeProvider()
    store = Mock(spec=HistoryStore)
    seeded = tuple(Message(role="user", content="Synthetic") for _ in range(256))
    snapshot = Snapshot("synthetic-conversation", 1, seeded)
    store.read.side_effect = lambda binding, cid: snapshot
    store.receipt.return_value = None

    def append(
        binding: Binding,
        cid: str,
        request_id: str,
        fingerprint: str,
        expected_revision: int,
        messages: tuple[Message, ...],
        finish_reason: str,
        *,
        card_sha256: str | None = None,
    ) -> Receipt:
        nonlocal snapshot
        assert cid == snapshot.conversation_id and expected_revision == snapshot.revision
        snapshot = Snapshot(cid, expected_revision + 1, (*snapshot.messages, *messages))
        return Receipt(
            fingerprint, snapshot.revision, messages[-1], finish_reason, card_sha256=card_sha256
        )

    store.append.side_effect = append
    http = TestClient(
        create_app(
            Inference((character("synthetic"),), provider),
            history_store=store,
            history_policy=SyntheticPolicy(),
        ),
        base_url="http://127.0.0.1",
    )
    url = f"/v1/characters/synthetic/conversations/{snapshot.conversation_id}"
    response = http.post(url + "/completions", json=turn(expected_revision=1).model_dump())
    assert response.status_code == 200
    restored = http.get(url).json()
    assert restored["revision"] == 2 and len(restored["messages"]) == 258
    assert provider.calls[0][1]["messages"][1:-1] == [
        message.model_dump(exclude_none=True) for message in seeded
    ]
    store.append.assert_called_once()


@pytest.mark.parametrize("count", [129, 257])
async def test_stream_accepts_contiguous_tool_indices_above_previous_limit(count: int) -> None:
    provider = FakeProvider()
    service = Conversations(
        Inference((character("synthetic"),), provider), cast(HistoryStore, None)
    )
    provider.chunks = [
        chunk(
            {
                "tool_calls": [
                    {
                        "index": i,
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": "synthetic", "arguments": "{"},
                    }
                ]
            }
        )
        for i in range(count)
    ]
    # Later fragments can revisit existing indices, including those above 127.
    provider.chunks += [
        chunk({"tool_calls": [{"index": i, "function": {"arguments": "}"}}]})
        for i in reversed(range(count))
    ]
    provider.chunks.append(chunk({}, "tool_calls"))
    message, finish = await service._stream(
        service.inference.characters["synthetic"].config.profile, {}
    )
    assert finish == "tool_calls" and message.tool_calls is not None
    assert [call.id for call in message.tool_calls] == [f"call_{i}" for i in range(count)]
    assert all(call.function.arguments == "{}" for call in message.tool_calls)
    assert provider.closed


@pytest.mark.parametrize("indices", [[1], [0, 2], [-1], [True], [0.0], ["0"]])
async def test_stream_rejects_noncontiguous_or_invalid_tool_indices(indices: list[Any]) -> None:
    provider = FakeProvider()
    service = Conversations(
        Inference((character("synthetic"),), provider), cast(HistoryStore, None)
    )
    provider.chunks = [chunk({"tool_calls": [{"index": index}]}) for index in indices]
    # No finish chunk: rejection must happen at the invalid index, before EOF.
    with pytest.raises(ValueError, match="^invalid tool index$"):
        await service._stream(service.inference.characters["synthetic"].config.profile, {})
    assert provider.closed


@pytest.mark.parametrize("extra", [-1, 0, 1])
@pytest.mark.parametrize("tools", [False, True])
async def test_exact_stream_boundary(
    monkeypatch: pytest.MonkeyPatch, extra: int, tools: bool
) -> None:
    provider = FakeProvider()
    service = Conversations(
        Inference((character("synthetic"),), provider), cast(HistoryStore, None)
    )
    content = '日本😀"\\\n\t\x00' * 3
    calls: dict[int, Any] = {
        0: {
            "id": "call_a",
            "type": "function",
            "function": {
                "name": "weather",
                "arguments": json.dumps({"city": content}, ensure_ascii=False),
            },
        }
    }
    expected = ["" if tools else content, calls if tools else {}]
    limit = len(json.dumps(expected, ensure_ascii=False).encode("utf-8"))
    monkeypatch.setattr(conversations, "MAX_BYTES", limit + extra)
    if tools:
        provider.chunks = [
            chunk({"tool_calls": [{"index": 0, "id": "call_", "function": {"name": "wea"}}]})
        ]
        provider.chunks += [
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": char}}]})
            for char in calls[0]["function"]["arguments"]
        ]
        provider.chunks += [
            chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "ther"}}]})
        ]
    else:
        provider.chunks = [chunk({"content": char}) for char in content]
    provider.chunks.append(chunk({}, "tool_calls" if tools else "stop"))
    profile = service.inference.characters["synthetic"].config.profile
    if extra < 0:
        with pytest.raises(CoreError) as error:
            await service._stream(profile, {})
        assert (error.value.status, error.value.code) == (413, "history_limit")
    else:
        message, _ = await service._stream(profile, {})
        if tools:
            assert message.tool_calls and message.tool_calls[0].model_dump() == calls[0]
        else:
            assert message.content == content
    assert provider.closed


async def test_tiny_chunks_have_linear_serialization_work(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    service = Conversations(
        Inference((character("synthetic"),), provider), cast(HistoryStore, None)
    )
    count = 10000
    provider.chunks = [chunk({"content": "界"}) for _ in range(count)] + [chunk({}, "stop")]
    original = json.dumps
    processed = 0

    def measured(value: Any, **kwargs: Any) -> str:
        nonlocal processed
        result = original(value, **kwargs)
        processed += len(result)
        return result

    monkeypatch.setattr(json, "dumps", measured)
    message, finish = await service._stream(
        service.inference.characters["synthetic"].config.profile, {}
    )
    assert message.content == "界" * count and finish == "stop"
    assert processed < count * 8  # Work is linear, independent of wall-clock timing.
    assert provider.closed


@pytest.mark.parametrize("extra", [-1, 0, 1])
@pytest.mark.parametrize("count", [12, 129])
async def test_multiple_tool_structural_byte_boundary(
    monkeypatch: pytest.MonkeyPatch, extra: int, count: int
) -> None:
    provider = FakeProvider()
    service = Conversations(
        Inference((character("synthetic"),), provider), cast(HistoryStore, None)
    )
    calls = {
        i: {
            "id": f"call_{i}",
            "type": "function",
            "function": {"name": "weather", "arguments": "{}"},
        }
        for i in range(count)
    }
    exact = len(json.dumps(["", calls], ensure_ascii=False).encode())
    monkeypatch.setattr(conversations, "MAX_BYTES", exact + extra)
    provider.chunks = [chunk({"tool_calls": [{"index": i, **call}]}) for i, call in calls.items()]
    provider.chunks.append(chunk({}, "tool_calls"))
    profile = service.inference.characters["synthetic"].config.profile
    if extra < 0:
        with pytest.raises(CoreError) as error:
            await service._stream(profile, {})
        assert error.value.code == "history_limit"
    else:
        message, _ = await service._stream(profile, {})
        assert message.tool_calls and len(message.tool_calls) == count
    assert provider.closed

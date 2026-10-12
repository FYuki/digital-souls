import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from digital_souls_core.api import create_app
from digital_souls_core.application import CoreError, Inference

from .support import CALL, ROOT, TOOL, FakeProvider, character, chunk, completion

pytestmark = pytest.mark.it1


def client(provider: FakeProvider) -> TestClient:
    return TestClient(
        create_app(Inference((character(), character("other")), provider)),
        base_url="http://127.0.0.1",
    )


def request_body(**extra: Any) -> dict[str, Any]:
    return {
        "model": "miori-alias",
        "messages": [{"role": "user", "content": "こんにちは"}],
        **extra,
    }


@pytest.mark.parametrize("endpoint", ["chat", "character"])
@pytest.mark.parametrize("stream", [False, True])
def test_stateless_accepts_messages_and_tools_above_previous_limits(
    endpoint: str, stream: bool
) -> None:
    fake = FakeProvider()
    messages = [{"role": "user", "content": "Synthetic"}] * 257
    tools = [
        {"type": "function", "function": {"name": f"synthetic_{i}", "parameters": {}}}
        for i in range(129)
    ]
    selector = {"model": "miori-alias"} if endpoint == "chat" else {"character_id": "miori"}
    response = client(fake).post(
        f"/v1/{endpoint}/completions",
        json={**selector, "messages": messages, "tools": tools, "stream": stream},
    )
    assert response.status_code == 200
    assert fake.calls[0][1]["messages"][1:] == messages
    assert fake.calls[0][1]["tools"] == tools
    if stream:
        assert response.text.endswith("data: [DONE]\n\n") and fake.closed
    else:
        assert response.json() == fake.response


def test_normal_chat_both_entries_same_context_and_pinned_metadata() -> None:
    fake = FakeProvider()
    http = client(fake)
    response = http.post("/v1/chat/completions", json=request_body())
    explicit = http.post(
        "/v1/character/completions",
        json={"character_id": "miori", "messages": [{"role": "user", "content": "こんにちは"}]},
    )
    assert response.status_code == explicit.status_code == 200
    assert response.json() == explicit.json() == fake.response
    assert response.headers["x-character-id"] == "miori"
    assert response.headers["x-character-config-version"] == "miori-runtime-1"
    assert response.headers["x-inference-profile"] == "miori-profile"
    assert fake.calls[0] == fake.calls[1]
    profile, payload = fake.calls[0]
    assert profile.model == "openai/miori-test-model"
    card = json.loads((ROOT / "characters/miori/miori.card.json").read_text())
    assert card["data"]["system_prompt"] in payload["messages"][0]["content"]
    assert "model" not in payload and "character_id" not in payload


@pytest.mark.parametrize(
    "choice", ["auto", "required", "none", {"type": "function", "function": {"name": "weather"}}]
)
def test_tool_call_and_result_round_trip(choice: Any) -> None:
    fake = FakeProvider()
    fake.response = completion(tool=True)
    http = client(fake)
    first = http.post("/v1/chat/completions", json=request_body(tools=[TOOL], tool_choice=choice))
    assert first.status_code == 200
    returned = first.json()["choices"][0]["message"]
    assert returned["tool_calls"] == [CALL]
    assert fake.calls[0][1]["tools"] == [TOOL]
    assert fake.calls[0][1]["tool_choice"] == choice
    messages = [
        *request_body()["messages"],
        returned,
        {"role": "tool", "content": '{"temperature":20}', "tool_call_id": "call_42"},
    ]
    fake.response = completion()
    second = http.post("/v1/chat/completions", json=request_body(messages=messages, tools=[TOOL]))
    assert second.status_code == 200
    assert fake.calls[-1][1]["messages"][1:] == messages
    assert len(fake.calls) == 2  # Core never executes an agent/tool loop.


def test_stream_text_and_fragmented_tool_arguments() -> None:
    fake = FakeProvider()
    http = client(fake)
    response = http.post("/v1/chat/completions", json=request_body(stream=True))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "こんにちは" in response.text and response.text.endswith("data: [DONE]\n\n")
    assert fake.closed
    fake.chunks = [
        chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_42",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{ "city":'},
                    }
                ]
            }
        ),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": ' "東京" }'}}]}),
        chunk({}, "tool_calls"),
    ]
    response = http.post("/v1/chat/completions", json=request_body(stream=True, tools=[TOOL]))
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert events == fake.chunks


@pytest.mark.parametrize(
    "status,code",
    [(429, "provider_rate_limit"), (504, "provider_timeout"), (502, "provider_error")],
)
def test_initial_and_midstream_errors(status: int, code: str) -> None:
    fake = FakeProvider()
    http = client(fake)
    fake.error = CoreError(status, code, "safe message")
    for streaming in (False, True):
        response = http.post("/v1/chat/completions", json=request_body(stream=streaming))
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
    fake.error = None
    fake.mid_error = CoreError(status, code, "safe message")
    response = http.post("/v1/chat/completions", json=request_body(stream=True))
    assert "event: error\n" in response.text and code in response.text
    assert "[DONE]" not in response.text
    assert fake.closed


def test_invalid_parameters_selectors_and_self_asserted_scope() -> None:
    fake = FakeProvider()
    http = client(fake)
    extras: tuple[dict[str, Any], ...] = (
        {"model": "openai/gpt-4o-mini"},
        {"api_key": "DO_NOT_ECHO"},
        {"audience": "public"},
        {"subject": "owner"},
        {"client": "streaming"},
        {"inference_profile": "other-profile"},
        {"response_format": {}},
        {"n": 1},
    )
    for extra in extras:
        response = http.post("/v1/chat/completions", json=request_body(**extra))
        assert response.status_code == 400
        assert "DO_NOT_ECHO" not in response.text
    assert http.post("/v1/chat/completions", json=request_body(model="absent")).status_code == 404
    assert not fake.calls


def test_empty_stream_errors_and_capability_metadata() -> None:
    fake = FakeProvider()
    fake.chunks = []
    http = client(fake)
    response = http.post("/v1/chat/completions", json=request_body(stream=True))
    assert response.status_code == 502 and response.json()["error"]["code"] == "empty_stream"
    assert fake.closed
    data = http.get("/v1/characters/miori").json()
    assert data["provider_model"] == "openai/miori-test-model"
    assert data["model_context_window_tokens"] is None
    assert "context_budget_bytes" not in data
    assert data["prompt_budget"]["total"] == 28672
    assert data["memory_floor"] == data["history_floor"] == 3
    assert http.get("/v1/models").json()["data"][0]["id"] == "miori-alias"


def test_default_config_is_offline_and_has_no_implicit_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CORE_CHARACTER_CONFIG", raising=False)
    http = TestClient(create_app(), base_url="http://127.0.0.1")
    assert http.get("/v1/models").json()["data"] == []
    monkeypatch.setenv("CORE_CHARACTER_CONFIG", str(ROOT / "examples/characters.json"))
    http = TestClient(create_app(), base_url="http://127.0.0.1")
    response = http.post("/v1/chat/completions", json=request_body(model="miori"))
    assert response.status_code == 403

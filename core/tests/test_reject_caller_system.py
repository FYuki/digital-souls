"""ADR 0031 rejects caller system before any inference side effect."""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from digital_souls_core.api import create_app
from digital_souls_core.application import Inference
from digital_souls_core.contracts import Message
from digital_souls_core.prompt_builder import split_messages

from .support import FakeProvider, character
from .test_prompt_assembly_api import configured, http_setup


@pytest.mark.ut
@pytest.mark.parametrize("position", [0, 1, 2])
def test_split_messages_rejects_system_without_echo(position: int) -> None:
    messages = [Message(role="user", content="old"), Message(role="user", content="now")]
    messages.insert(position, Message(role="system", content="private-marker"))
    with pytest.raises(ValueError) as caught:
        split_messages(messages)
    assert "private-marker" not in str(caught.value)


@pytest.mark.ut
def test_obsolete_caller_budget_is_startup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValidationError, match="caller_instructions"):
        configured(tmp_path, prompt_budget={"caller_instructions": 8192})
    monkeypatch.setenv("CORE_CHARACTER_CONFIG", str(tmp_path / "config.json"))
    with pytest.raises(ValidationError, match="caller_instructions"):
        create_app()


@pytest.mark.it1
@pytest.mark.parametrize("entry", ["chat", "character", "history"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "content", ["private-marker", [{"type": "text", "text": "private-marker"}]]
)
@pytest.mark.parametrize("position", [0, 1, 2])
def test_system_rejected_before_privacy_context_tokens_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    stream: bool,
    content: object,
    position: int,
) -> None:
    http, provider, inference, store = http_setup(configured(tmp_path), ())
    authorize = AsyncMock(side_effect=AssertionError("privacy called"))
    monkeypatch.setattr(inference.privacy, "authorize", authorize)
    context = AsyncMock(side_effect=AssertionError("context called"))
    monkeypatch.setattr(inference.context, "context", context)
    memory = AsyncMock(side_effect=AssertionError("memory called"))
    monkeypatch.setattr(inference.memory_context, "context", memory)
    counter = Mock(side_effect=AssertionError("token counter created"))
    monkeypatch.setattr("digital_souls_core.application.ByteTokenCounter", counter)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "now"},
    ]
    messages.insert(position, {"role": "system", "content": content})
    selector: dict[str, Any]
    if entry == "history":
        path = "/v1/characters/synthetic/conversations/synthetic/completions"
        selector = {"request_id": "r", "expected_revision": 1}
    else:
        path = f"/v1/{entry}/completions"
        selector = (
            {"model": "synthetic-alias"} if entry == "chat" else {"character_id": "synthetic"}
        )
    response = http.post(path, json={**selector, "messages": messages, "stream": stream})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "system_message_unsupported"
    assert "private-marker" not in response.text
    assert response.headers["content-type"].startswith("application/json")
    authorize.assert_not_called()
    context.assert_not_called()
    memory.assert_not_called()
    counter.assert_not_called()
    assert not provider.calls
    store.assert_not_called()
    assert not store.method_calls


@pytest.mark.it1
def test_history_other_invalid_turn_unchanged(tmp_path: Path) -> None:
    http, provider, _, store = http_setup(configured(tmp_path), ())
    response = http.post(
        "/v1/characters/synthetic/conversations/synthetic/completions",
        json={
            "request_id": "r",
            "expected_revision": 1,
            "messages": [{"role": "assistant", "content": "bad"}],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_turn"
    assert not provider.calls and not store.method_calls


@pytest.mark.it1
def test_metadata_has_no_caller_budget() -> None:
    http = TestClient(
        create_app(Inference((character("synthetic"),), FakeProvider())),
        base_url="http://127.0.0.1",
    )
    response = http.get("/v1/characters/synthetic")
    assert response.status_code == 200
    assert "caller_instructions" not in response.json()["prompt_budget"]


@pytest.mark.it1
@pytest.mark.parametrize("entry", ["chat", "character", "history"])
@pytest.mark.parametrize("stream", [False, True])
def test_system_rejection_takes_precedence_over_invalid_tool_sequence(
    tmp_path: Path, entry: str, stream: bool
) -> None:
    http, provider, _, store = http_setup(configured(tmp_path), ())
    selector: dict[str, Any] = (
        {"model": "synthetic-alias"} if entry == "chat" else {"character_id": "synthetic"}
    )
    path = f"/v1/{entry}/completions"
    if entry == "history":
        path = "/v1/characters/synthetic/conversations/synthetic/completions"
        selector = {"request_id": "r", "expected_revision": 1}
    response = http.post(
        path,
        json={
            **selector,
            "stream": stream,
            "messages": [
                {"role": "system", "content": "private-marker"},
                {"role": "tool", "content": "orphan", "tool_call_id": "missing"},
            ],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "system_message_unsupported"
    assert "private-marker" not in response.text
    assert not provider.calls and not store.method_calls

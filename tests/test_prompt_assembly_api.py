"""Shared HTTP assembly with fake history, retrieval and provider only."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from digital_souls_core.api import create_app
from digital_souls_core.application import Inference, Prepared
from digital_souls_core.character import (
    AccessScope,
    Character,
    CharacterConfig,
    GuardedContext,
    load_characters,
)
from digital_souls_core.contracts import Message
from digital_souls_core.history import Binding, HistoryStore, Receipt, Snapshot
from digital_souls_core.privacy import PrivacyPolicy

from .support import CALL, FakeProvider, character

pytestmark = pytest.mark.it1


REGIONS = (
    "total",
    "character",
    "lore",
    "memory",
    "history",
    "current_input",
    "post_history",
)


def configured(tmp_path: Path, **options: Any) -> Character:
    card = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "not-in-prompt",
            "description": " persona ",
            "personality": " ",
            "post_history_instructions": "post",
        },
    }
    (tmp_path / "card.json").write_text(json.dumps(card))
    config = character("synthetic").config.model_dump(mode="json", exclude_none=True)
    config.update(card_path="card.json", **options)
    path = tmp_path / "config.json"
    path.write_text(json.dumps([config]))
    return load_characters(path)[0]


def test_post_history_card_loads_and_name_is_not_in_prompt(tmp_path: Path) -> None:
    char = configured(tmp_path)
    fake = FakeProvider()
    http = TestClient(create_app(Inference((char,), fake)), base_url="http://127.0.0.1")
    response = http.post(
        "/v1/chat/completions",
        json={"model": "synthetic-alias", "messages": [{"role": "user", "content": "now"}]},
    )
    assert response.status_code == 200
    assert fake.calls[0][1]["messages"] == [
        {"role": "system", "content": "## キャラクター概要\npersona"},
        {"role": "system", "content": "post"},
        {"role": "user", "content": "now"},
    ]


def test_obsolete_byte_budget_is_a_startup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValidationError, match="context_budget_bytes"):
        configured(tmp_path, context_budget_bytes=16384)
    monkeypatch.setenv("CORE_CHARACTER_CONFIG", str(tmp_path / "config.json"))
    with pytest.raises(ValidationError, match="context_budget_bytes"):
        create_app()


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("invalid", [0, -1, True, "10", 1.5])
def test_prompt_budget_is_strict_and_positive(region: str, invalid: object) -> None:
    config = character().config.model_dump(exclude_none=True)
    config["prompt_budget"] = {**dict.fromkeys(REGIONS, 10000), region: invalid}
    with pytest.raises(ValidationError):
        CharacterConfig.model_validate(config)


@pytest.mark.parametrize("field", ["memory_floor", "history_floor"])
@pytest.mark.parametrize("invalid", [0, -1, True, "3", 1.5])
def test_prompt_floors_are_strict_and_positive(field: str, invalid: object) -> None:
    config = character().config.model_dump(exclude_none=True)
    with pytest.raises(ValidationError):
        CharacterConfig.model_validate({**config, field: invalid})


class Classifier:
    async def safe(self, value: object, policy_version: str) -> bool:
        return True


class Memory:
    def __init__(self, policy: PrivacyPolicy) -> None:
        self.policy = policy
        self.revoked = False

    async def context(
        self, char: Character, scope: AccessScope, user_text: str, *, authorized: Callable[[], bool]
    ) -> GuardedContext:
        items = tuple(json.dumps({"text": f"memory-{i}"}) for i in range(5))
        return GuardedContext(
            "[" + ", ".join(items) + "]",
            lambda: authorized() and not self.revoked,
            self.policy,
            items=items,
        )


class CapturingInference(Inference):
    prepared: Prepared
    revoke = False

    async def prepare(self, *args: Any, **kwargs: Any) -> Prepared:
        self.prepared = await super().prepare(*args, **kwargs)
        if self.revoke:
            assert isinstance(self.memory_context, Memory)
            self.memory_context.revoked = True
        return self.prepared


def http_setup(
    char: Character, seeded: tuple[Message, ...]
) -> tuple[TestClient, FakeProvider, CapturingInference, Mock]:
    provider = FakeProvider()
    privacy = PrivacyPolicy(Classifier())
    privacy.configure(
        {
            Binding(AccessScope(), char.config.character_id): frozenset(
                {"local", "external", "history", "memory"}
            )
        }
    )
    inference = CapturingInference(
        (char,), provider, privacy=privacy, memory_context=Memory(privacy)
    )
    store = Mock(spec=HistoryStore)
    store.read.return_value = Snapshot("synthetic", 1, seeded)
    store.receipt.return_value = None
    store.append.side_effect = (
        lambda binding,
        cid,
        request_id,
        fingerprint,
        revision,
        messages,
        finish,
        *,
        card_sha256: Receipt(
            fingerprint, revision + 1, messages[-1], finish, card_sha256=card_sha256
        )
    )
    http = TestClient(
        create_app(inference, history_store=store, history_policy=privacy),
        base_url="http://127.0.0.1",
    )
    return http, provider, inference, store


def post(
    http: TestClient, entry: str, seeded: tuple[Message, ...], current: list[dict[str, Any]]
) -> Any:
    if entry == "chat":
        return http.post(
            "/v1/chat/completions",
            json={
                "model": "synthetic-alias",
                "messages": [m.model_dump(exclude_none=True) for m in seeded] + current,
            },
        )
    return http.post(
        "/v1/characters/synthetic/conversations/synthetic/completions",
        json={"request_id": "r2", "expected_revision": 1, "messages": current},
    )


def seeded_turns() -> tuple[Message, ...]:
    return tuple(
        Message.model_validate(m)
        for m in [
            {"role": "assistant", "content": "greeting"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old-answer"},
            {"role": "user", "content": "recent"},
            {"role": "assistant", "tool_calls": [CALL]},
            {"role": "tool", "tool_call_id": "call_42", "content": "tool-result"},
            {"role": "assistant", "content": "recent-answer"},
        ]
    )


@pytest.mark.parametrize("entry", ["chat", "history"])
def test_http_order_and_whole_tool_turn_trimming(tmp_path: Path, entry: str) -> None:
    limits = dict.fromkeys(REGIONS, 10000)
    limits["history"] = 150
    char = configured(tmp_path, prompt_budget=limits)
    seeded = seeded_turns()
    http, provider, inference, store = http_setup(char, seeded)
    current = [{"role": "user", "content": "now"}]
    response = post(http, entry, seeded, current)
    assert response.status_code == 200
    messages = provider.calls[0][1]["messages"]
    assert messages[0]["content"].startswith("## キャラクター概要\npersona")
    assert "not-in-prompt" not in str(messages)
    offset = 1
    if entry == "history":
        assert messages[offset]["role"] == "user" and "memory-0" in messages[offset]["content"]
        assert messages[offset]["content"].index("memory-0") < messages[offset]["content"].index(
            "memory-4"
        )
        offset += 1
        store.append.assert_called_once()
    assert messages[offset:-2] == [m.model_dump(exclude_none=True) for m in seeded[3:]]
    assert messages[-2:] == [
        {"role": "system", "content": "post"},
        {"role": "user", "content": "now"},
    ]
    assert inference.prepared.usage is not None
    assert inference.prepared.usage.dropped["history"] == 2
    assert (
        "usage" not in response.json()
        if entry == "history"
        else response.json() == provider.response
    )


@pytest.mark.parametrize("entry", ["chat", "history"])
@pytest.mark.parametrize("region", ["character", "history", "current_input", "total"])
def test_required_overflow_returns_only_region(tmp_path: Path, entry: str, region: str) -> None:
    limits = {**dict.fromkeys(REGIONS, 10000), region: 1}
    char = configured(tmp_path, prompt_budget=limits)
    seeded = seeded_turns()
    http, provider, _, store = http_setup(char, seeded)
    response = post(http, entry, seeded, [{"role": "user", "content": "now"}])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "prompt_budget_exceeded"
    assert response.json()["error"]["message"] == region
    assert not provider.calls and not store.append.called


@pytest.mark.parametrize("memory_budget", [10000, 1])
def test_history_rejects_system_and_revocation_still_blocks_dispatch(
    tmp_path: Path, memory_budget: int
) -> None:
    char = configured(
        tmp_path, prompt_budget={**dict.fromkeys(REGIONS, 10000), "memory": memory_budget}
    )
    seeded = seeded_turns()
    http, provider, inference, store = http_setup(char, seeded)
    assert (
        post(http, "history", seeded, [{"role": "system", "content": "caller"}]).status_code == 400
    )
    inference.revoke = True
    response = post(http, "history", seeded, [{"role": "user", "content": "now"}])
    assert response.status_code == 403 and response.json()["error"]["code"] == "context_revoked"
    assert not provider.calls and not store.append.called


def test_history_memory_reduction_keeps_top_ranked_json_items(tmp_path: Path) -> None:
    limits = dict.fromkeys(REGIONS, 10000)
    limits["memory"] = 410
    char = configured(tmp_path, prompt_budget=limits)
    http, provider, inference, _ = http_setup(char, ())
    response = post(http, "history", (), [{"role": "user", "content": "now"}])
    assert response.status_code == 200
    data = json.loads(provider.calls[0][1]["messages"][1]["content"].split("\n", 1)[1])
    assert 0 < len(data) < 5
    assert data == [{"text": f"memory-{i}"} for i in range(len(data))]
    assert inference.prepared.usage.dropped["memory"] == 5 - len(data)


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "assistant", "content": "greeting"}, {"role": "user", "content": "now"}],
        [{"role": "assistant", "content": "greeting"}],
        [{"role": "user", "content": "now"}, {"role": "assistant", "content": "answer"}],
    ],
)
def test_chat_keeps_accepted_sequences(tmp_path: Path, messages: list[dict[str, Any]]) -> None:
    char = configured(tmp_path)
    http, provider, _, _ = http_setup(char, ())
    response = post(http, "chat", (), messages)
    assert response.status_code == 200
    assert [m for m in provider.calls[0][1]["messages"] if m["role"] != "system"] == [
        m for m in messages if m["role"] != "system"
    ]

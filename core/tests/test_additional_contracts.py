from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from digital_souls_core.api import create_app
from digital_souls_core.application import Inference
from digital_souls_core.character import (
    AccessScope,
    Character,
    EmptyContext,
    Profile,
    load_characters,
)

from .fixture_server import create_fixture_app
from .support import FakeProvider, character

pytestmark = pytest.mark.it1


def test_external_client_fixture_round_trip() -> None:
    http = TestClient(create_fixture_app(), base_url="http://127.0.0.1")
    messages: list[dict[str, Any]] = [{"role": "user", "content": "run fixture"}]
    body = {
        "character_id": "miori",
        "messages": messages,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "fixture_ping",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }
    first = http.post("/v1/character/completions", json=body)
    assert first.status_code == 200
    messages.extend(
        [
            first.json()["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "fixture_call_1", "content": "pong"},
        ]
    )
    second = http.post("/v1/character/completions", json=body)
    assert second.status_code == 200
    assert second.json()["choices"][0]["message"]["content"] == "fixture tool result received"


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_token_limit_normalization(field: str) -> None:
    fake = FakeProvider()
    http = TestClient(create_app(Inference((character(),), fake)), base_url="http://127.0.0.1")
    body: dict[str, Any] = {
        "model": "miori-alias",
        "messages": [{"role": "user", "content": "hi"}],
        field: 99,
    }
    assert http.post("/v1/chat/completions", json=body).status_code == 200
    assert fake.calls[0][1]["max_completion_tokens"] == 99
    assert "max_tokens" not in fake.calls[0][1]
    body.update(max_tokens=99, max_completion_tokens=99)
    assert http.post("/v1/chat/completions", json=body).status_code == 400


def test_context_failure_does_not_call_provider_or_echo_private_data() -> None:
    class BrokenContext(EmptyContext):
        async def context(self, char: Character, scope: AccessScope, user_text: str) -> str:
            raise RuntimeError("SYNTHETIC_PRIVATE_DATA")

    fake = FakeProvider()
    http = TestClient(
        create_app(Inference((character(),), fake, BrokenContext())), base_url="http://127.0.0.1"
    )
    response = http.post(
        "/v1/chat/completions",
        json={"model": "miori-alias", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 500 and "SYNTHETIC_PRIVATE_DATA" not in response.text
    assert not fake.calls


def test_denied_profile_does_not_retrieve_context() -> None:
    class ForbiddenContext(EmptyContext):
        async def context(self, char: Character, scope: AccessScope, user_text: str) -> str:
            pytest.fail("denied policy must prevent context retrieval")

    char = character()
    denied = replace(
        char,
        config=char.config.model_copy(
            update={
                "profile": char.config.profile.model_copy(update={"external_send_allowed": False})
            }
        ),
    )
    http = TestClient(
        create_app(Inference((denied,), FakeProvider(), ForbiddenContext())),
        base_url="http://127.0.0.1",
    )
    response = http.post(
        "/v1/chat/completions",
        json={"model": "miori-alias", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 403


def test_invalid_policy_is_not_coerced_and_invalid_configuration_fails_startup(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate(
            {"profile_id": "test", "model": "openai/test", "external_send_allowed": "true"}
        )
    with pytest.raises(ValidationError):
        Profile.model_validate(
            {"profile_id": "test", "model": "openai/test", "allowed_parameters": ["unknown"]}
        )
    config = tmp_path / "config.json"
    config.write_text("[]")
    with pytest.raises(ValueError):
        load_characters(config)


def test_stateless_http_character_separation() -> None:
    fake = FakeProvider()
    http = TestClient(
        create_app(Inference((character(), character("other")), fake)), base_url="http://127.0.0.1"
    )
    for alias in ("miori-alias", "other-alias", "miori-alias"):
        assert (
            http.post(
                "/v1/chat/completions",
                json={"model": alias, "messages": [{"role": "user", "content": alias}]},
            ).status_code
            == 200
        )
    for index, (profile, payload) in enumerate(fake.calls):
        expected = "other" if index == 1 else "miori"
        assert profile.profile_id == f"{expected}-profile"
        assert len(payload["messages"]) == 2
        assert ("光織として" in payload["messages"][0]["content"]) == (expected == "miori")

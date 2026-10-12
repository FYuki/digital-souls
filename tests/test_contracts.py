import asyncio
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from digital_souls_core.application import CoreError, Inference
from digital_souls_core.character import AccessScope, Character, EmptyContext, Profile
from digital_souls_core.contracts import CompletionInput

from .support import CALL, TOOL, FakeProvider, character

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "update",
    [
        {"api_key": "synthetic-secret"},
        {"api_base": "https://invalid.example"},
        {"response_format": {"type": "json_object"}},
        {"stream_options": {}},
        {"n": 2},
        {"max_tokens": 0},
        {"temperature": float("nan")},
        {"stream": "true"},
        {"messages": [{"role": "tool", "content": "result", "tool_call_id": "missing"}]},
        {"messages": [{"role": "assistant", "tool_calls": [CALL]}]},
        {"tool_choice": "auto"},
        {"tools": [TOOL, TOOL]},
        {"tools": [TOOL], "tool_choice": {"type": "function", "function": {"name": "absent"}}},
    ],
)
def test_invalid_contract(update: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CompletionInput.model_validate({"messages": [{"role": "user", "content": "hi"}], **update})


async def test_context_lore_and_token_budget() -> None:
    char = character()
    inference = Inference((char,), FakeProvider())
    request = CompletionInput.model_validate(
        {"messages": [{"role": "user", "content": "自己紹介して"}]}
    )
    result = await inference.prepare("miori", request, alias=False)
    prompt = result.payload["messages"][0]["content"]
    assert "光織として" in prompt and "柔らかな丁寧語" in prompt
    assert "太陽の光、薄明光線" in prompt
    assert "光織の外見:" not in prompt
    tiny = replace(
        char,
        config=char.config.model_copy(
            update={"prompt_budget": char.config.prompt_budget.model_copy(update={"character": 1})}
        ),
    )
    with pytest.raises(CoreError, match="^character$"):
        await Inference((tiny,), FakeProvider()).prepare("miori", request, alias=False)


async def test_lore_secondary_keys_and_latest_user_only() -> None:
    inference = Inference((character(),), FakeProvider())

    async def prompt(texts: list[str]) -> str:
        result = await inference.prepare(
            "miori",
            CompletionInput.model_validate(
                {"messages": [{"role": "user", "content": text} for text in texts]}
            ),
            alias=False,
        )
        return str(result.payload["messages"][0]["content"])

    assert "光織と黄昏:" not in await prompt(["黄昏の時刻"])
    assert "光織と黄昏:" in await prompt(["あなたと黄昏の関係"])
    assert "光織と黄昏:" not in await prompt(["あなたと黄昏の関係", "計算して"])


async def test_policy_and_unknown_capabilities_fail_closed() -> None:
    char = character()
    denied = replace(
        char,
        config=char.config.model_copy(
            update={"profile": Profile(profile_id="denied", model="openai/test")}
        ),
    )
    request = CompletionInput.model_validate({"messages": [{"role": "user", "content": "hi"}]})
    with pytest.raises(CoreError) as error:
        await Inference((denied,), FakeProvider()).prepare("miori", request, alias=False)
    assert error.value.status == 403
    unknown = replace(
        char,
        config=char.config.model_copy(
            update={
                "profile": char.config.profile.model_copy(
                    update={"allowed_parameters": frozenset()}
                )
            }
        ),
    )
    with pytest.raises(CoreError) as error:
        await Inference((unknown,), FakeProvider()).prepare(
            "miori", request.model_copy(update={"stream": True}), alias=False
        )
    assert error.value.code == "unsupported_parameter"


async def test_concurrent_context_is_keyed_and_snapshots_are_frozen() -> None:
    class Context(EmptyContext):
        async def context(self, char: Character, scope: AccessScope, user_text: str) -> str:
            assert scope == AccessScope()
            await asyncio.sleep(0)
            return f"context:{char.config.character_id}:{char.config.config_version}:{user_text}"

    chars = (character(), character("other"))
    inference = Inference(chars, FakeProvider(), Context())
    request = CompletionInput.model_validate(
        {"messages": [{"role": "user", "content": "synthetic"}]}
    )
    results = await asyncio.gather(
        *(inference.prepare(name, request, alias=False) for name in ("miori", "other") * 20)
    )
    for index, result in enumerate(results):
        expected = "miori" if index % 2 == 0 else "other"
        assert f"context:{expected}:" in result.payload["messages"][0]["content"]
        assert result.character.config.profile.model == f"openai/{expected}-test-model"
    with pytest.raises(ValidationError):
        chars[0].config.profile.model = "openai/changed"
    with pytest.raises(ValueError, match="duplicate"):
        Inference((chars[0], chars[0]), FakeProvider())

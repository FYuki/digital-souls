import copy
import hashlib
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from digital_souls_core.character import Character, Profile, SupportedParameter, load_characters
from digital_souls_core.prompt_builder import CharacterCard

ROOT = Path(__file__).resolve().parents[1]
PARAMETERS: frozenset[SupportedParameter] = frozenset(
    {"tools", "tool_choice", "stream", "temperature", "max_completion_tokens"}
)
CALL = {
    "id": "call_42",
    "type": "function",
    "function": {"name": "weather", "arguments": '{ "city": "東京" }'},
}
TOOL = {
    "type": "function",
    "function": {
        "name": "weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def character(name: str = "miori", *, native: bool = False) -> Character:
    sample = load_characters(ROOT / "examples/characters.json")[0]
    profile = Profile(
        profile_id=f"{name}-profile",
        model="openai/gpt-4o-mini-2024-07-18" if native else f"openai/{name}-test-model",
        allowed_parameters=PARAMETERS,
        external_send_allowed=True,
    )
    config = sample.config.model_copy(
        update={"character_id": name, "alias": f"{name}-alias", "profile": profile}
    )
    return Character(
        config,
        sample.card if name == "miori" else CharacterCard(system_prompt="Synthetic second persona"),
        character_book=sample.character_book if name == "miori" else None,
        card_sha256=(
            sample.card_sha256
            if name == "miori"
            else hashlib.sha256(b"Synthetic second persona").hexdigest()
        ),
    )


def completion(model: str = "test-model", tool: bool = False) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": "こんにちは"}
    if tool:
        message = {"role": "assistant", "tool_calls": [copy.deepcopy(CALL)]}
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": "tool_calls" if tool else "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[Profile, dict[str, Any]]] = []
        self.response = completion()
        self.chunks = [
            chunk({"role": "assistant"}),
            chunk({"content": "こんにちは"}),
            chunk({}, "stop"),
        ]
        self.error: Exception | None = None
        self.mid_error: Exception | None = None
        self.closed = False

    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((profile, copy.deepcopy(payload)))
        if self.error:
            raise self.error
        return copy.deepcopy(self.response)

    async def stream(
        self, profile: Profile, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        self.calls.append((profile, copy.deepcopy(payload)))
        try:
            if self.error:
                raise self.error
            for value in self.chunks:
                yield copy.deepcopy(value)
                if self.mid_error:
                    raise self.mid_error
        finally:
            self.closed = True

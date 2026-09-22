"""dict形式メッセージで構造化生成を呼ぶdomain port向けの共通部品。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Protocol

from app.inference.contracts import (
    InferenceMessage,
    StructuredGenerationResult,
)


class StructuredChatClient(Protocol):
    """json_schema指定の構造化chatを提供するdomain port。"""

    def chat(
        self,
        messages: tuple[dict[str, str], ...],
        *,
        json_schema: dict[str, object],
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> str: ...


def to_inference_messages(
    messages: Iterable[Mapping[str, object]],
) -> tuple[InferenceMessage, ...]:
    converted: list[InferenceMessage] = []
    for message in messages:
        if set(message) != {"role", "content"}:
            raise ValueError("inference message fields are invalid")
        role = message["role"]
        content = message["content"]
        if not isinstance(role, str) or not isinstance(content, str):
            raise ValueError("inference message values must be strings")
        converted.append(InferenceMessage(role, content))
    return tuple(converted)


def structured_result_json(result: StructuredGenerationResult) -> str:
    return json.dumps(result.value, ensure_ascii=False, separators=(",", ":"))

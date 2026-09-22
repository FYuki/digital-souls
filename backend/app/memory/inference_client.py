from __future__ import annotations

import json
import math
from collections.abc import Mapping

from app.inference import (
    InferenceCaller,
    InferenceError,
    InferenceErrorCategory,
    InferenceMessage,
    InferenceRouter,
    InferenceSettings,
    InferenceTarget,
)
from app.inference.structured import (
    structured_result_json,
    to_inference_messages,
)


class StructuredMemoryInferenceClient:
    """Memory domainの構造化生成を共通Inference契約へ接続する。"""

    def __init__(
        self,
        *,
        router: InferenceRouter,
        caller: InferenceCaller,
        target: InferenceTarget,
        settings: InferenceSettings,
    ) -> None:
        self._router = router
        self._caller = caller
        self._target = target
        self._max_input_tokens = settings.target(target).max_input_tokens

    def close(self) -> None:
        return None

    def chat(
        self,
        messages: tuple[dict[str, str], ...],
        *,
        json_schema: dict[str, object],
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> str:
        del max_output_tokens
        converted = to_inference_messages(messages)
        if _conservative_token_count(converted, json_schema) > self._max_input_tokens:
            raise InferenceError(
                InferenceErrorCategory.INVALID_REQUEST,
                retryable=False,
            )
        result = self._router.generate_structured(
            caller=self._caller,
            target=self._target,
            messages=converted,
            response_schema=json_schema,
            timeout_seconds=timeout_seconds,
        )
        return structured_result_json(result)


class MemoryInferenceEmbedder:
    """Embedding Targetを1入力のMemory用callableとして公開する。"""

    def __init__(
        self,
        *,
        router: InferenceRouter,
        settings: InferenceSettings,
    ) -> None:
        self._router = router
        reference = settings.target(InferenceTarget.EMBEDDING).reference
        self.provider_id = reference.provider_id
        self.model_id = reference.model_id

    def __call__(self, text: str) -> list[float]:
        result = self._router.embed(
            caller=InferenceCaller.MEMORY_INDEX,
            target=InferenceTarget.EMBEDDING,
            inputs=(text,),
        )
        if len(result.vectors) != 1:
            raise RuntimeError("memory embedding must return exactly one vector")
        return list(result.vectors[0])


def _conservative_token_count(
    messages: tuple[InferenceMessage, ...],
    response_schema: Mapping[str, object],
) -> int:
    serialized = json.dumps(
        {
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "response_schema": response_schema,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return max(1, math.ceil(len(serialized) / 2 * 1.2))

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from app.inference.contracts import InferenceTarget
from app.inference.errors import InferenceError, InferenceErrorCategory


class InferenceCaller(str, Enum):
    """Core内部でInferenceを利用できる固定呼出元。"""

    CHAT = "chat"
    SEMANTIC_PRIVACY = "semantic-privacy"
    MEMORY_EXTRACTION = "memory-extraction"
    MEMORY_CONSOLIDATION = "memory-consolidation"
    MEMORY_INDEX = "memory-index"
    SCREEN_VISION = "screen-vision"
    SCREEN_REFERENCE = "screen-reference"
    HEAVY_REASONING = "heavy-reasoning"
    TOOL_ROUTING = "tool-routing"
    CHARACTER_LIFE = "character-life"


CORE_TARGET_ALLOWLIST: Mapping[InferenceCaller, frozenset[InferenceTarget]] = {
    InferenceCaller.CHARACTER_LIFE: frozenset({InferenceTarget.CHARACTER_LIFE}),
    InferenceCaller.CHAT: frozenset({InferenceTarget.CHAT}),
    InferenceCaller.SEMANTIC_PRIVACY: frozenset({InferenceTarget.PRIVACY}),
    InferenceCaller.MEMORY_EXTRACTION: frozenset({InferenceTarget.MEMORY_EXTRACTION}),
    InferenceCaller.MEMORY_CONSOLIDATION: frozenset(
        {InferenceTarget.MEMORY_CONSOLIDATION}
    ),
    InferenceCaller.MEMORY_INDEX: frozenset({InferenceTarget.EMBEDDING}),
    InferenceCaller.SCREEN_VISION: frozenset({InferenceTarget.VISION}),
    InferenceCaller.SCREEN_REFERENCE: frozenset({InferenceTarget.CHAT}),
    InferenceCaller.HEAVY_REASONING: frozenset({InferenceTarget.HEAVY_REASONING}),
    InferenceCaller.TOOL_ROUTING: frozenset({InferenceTarget.TOOL_ROUTING}),
}


def authorize(caller: InferenceCaller, target: InferenceTarget) -> None:
    if target not in CORE_TARGET_ALLOWLIST[caller]:
        raise InferenceError(
            InferenceErrorCategory.ACCESS_DENIED,
            retryable=False,
        )

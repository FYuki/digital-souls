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
    HEAVY_REASONING = "heavy-reasoning"


CORE_TARGET_ALLOWLIST: Mapping[InferenceCaller, frozenset[InferenceTarget]] = {
    InferenceCaller.CHAT: frozenset({InferenceTarget.CHAT}),
    InferenceCaller.SEMANTIC_PRIVACY: frozenset({InferenceTarget.PRIVACY}),
    InferenceCaller.MEMORY_EXTRACTION: frozenset({InferenceTarget.MEMORY_EXTRACTION}),
    InferenceCaller.MEMORY_CONSOLIDATION: frozenset(
        {InferenceTarget.MEMORY_CONSOLIDATION}
    ),
    InferenceCaller.MEMORY_INDEX: frozenset({InferenceTarget.EMBEDDING}),
    InferenceCaller.HEAVY_REASONING: frozenset({InferenceTarget.HEAVY_REASONING}),
}


def authorize(caller: InferenceCaller, target: InferenceTarget) -> None:
    if target not in CORE_TARGET_ALLOWLIST[caller]:
        raise InferenceError(
            InferenceErrorCategory.ACCESS_DENIED,
            retryable=False,
        )

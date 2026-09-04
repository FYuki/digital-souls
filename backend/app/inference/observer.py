from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from app.inference.authorization import InferenceCaller
from app.inference.contracts import InferenceCapability, InferenceTarget
from app.inference.contracts import InferenceUsage, TokenEstimate
from app.inference.errors import InferenceErrorCategory


@dataclass(frozen=True)
class InferenceObservation:
    """本文や認証情報を含まないInference呼出し結果。"""

    request_id: str
    caller: InferenceCaller
    target: InferenceTarget
    capability: InferenceCapability
    provider_id: str
    model_id: str
    auth_kind: str
    latency_ms: float
    external_request_count: int
    token_estimate: TokenEstimate | None
    usage: InferenceUsage | None
    success: bool
    error_category: InferenceErrorCategory | None


InferenceObserver: TypeAlias = Callable[[InferenceObservation], None]


def ignore_inference_observation(_observation: InferenceObservation) -> None:
    return None

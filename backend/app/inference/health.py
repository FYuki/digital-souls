from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from threading import Lock

from app.inference.config import InferenceSettings, TARGET_DEFINITIONS
from app.inference.contracts import (
    InferenceCapability,
    InferenceTarget,
    TargetCriticality,
)
from app.inference.errors import InferenceErrorCategory


class InferenceTargetState(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    UNCONFIGURED = "unconfigured"
    INVALID = "invalid"


class InferenceVerification(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class InferenceTargetHealth:
    target: InferenceTarget
    state: InferenceTargetState
    verification: InferenceVerification
    required_capabilities: tuple[InferenceCapability, ...]
    error_category: InferenceErrorCategory | None
    last_checked_at: datetime | None

    def public_dict(self) -> dict[str, object]:
        return {
            "target": self.target.value,
            "state": self.state.value,
            "verification": self.verification.value,
            "required_capabilities": [
                capability.value for capability in self.required_capabilities
            ],
            "error_category": (
                None if self.error_category is None else self.error_category.value
            ),
            "last_checked_at": (
                None
                if self.last_checked_at is None
                else self.last_checked_at.isoformat().replace("+00:00", "Z")
            ),
        }


class InferenceHealth:
    """Provider詳細を保持せず、公開可能なTarget状態だけを管理する。"""

    def __init__(self, settings: InferenceSettings) -> None:
        self._lock = Lock()
        self._states = {
            target: InferenceTargetHealth(
                target=target,
                state=(
                    InferenceTargetState.DEGRADED
                    if target in settings.targets
                    else InferenceTargetState.UNCONFIGURED
                ),
                verification=InferenceVerification.UNVERIFIED,
                required_capabilities=tuple(
                    sorted(
                        definition.required_capabilities,
                        key=lambda capability: capability.value,
                    )
                ),
                error_category=None,
                last_checked_at=None,
            )
            for target, definition in TARGET_DEFINITIONS.items()
        }

    def record_success(self, target: InferenceTarget) -> None:
        self._record(target, InferenceTargetState.READY, None, verified=True)

    def record_failure(
        self,
        target: InferenceTarget,
        category: InferenceErrorCategory,
    ) -> None:
        state = (
            InferenceTargetState.INVALID
            if category
            in {
                InferenceErrorCategory.AUTHENTICATION_FAILED,
                InferenceErrorCategory.PERMISSION_DENIED,
                InferenceErrorCategory.MODEL_NOT_FOUND,
                InferenceErrorCategory.UNSUPPORTED_CAPABILITY,
                InferenceErrorCategory.INVALID_REQUEST,
                InferenceErrorCategory.ACCESS_DENIED,
            }
            else InferenceTargetState.DEGRADED
        )
        self._record(target, state, category, verified=False)

    def snapshot(self) -> tuple[InferenceTargetHealth, ...]:
        with self._lock:
            return tuple(self._states[target] for target in InferenceTarget)

    def is_ready(self) -> bool:
        with self._lock:
            return all(
                definition.criticality is not TargetCriticality.REQUIRED
                or self._states[target].state is InferenceTargetState.READY
                for target, definition in TARGET_DEFINITIONS.items()
            )

    def _record(
        self,
        target: InferenceTarget,
        state: InferenceTargetState,
        category: InferenceErrorCategory | None,
        *,
        verified: bool,
    ) -> None:
        with self._lock:
            previous = self._states[target]
            self._states[target] = InferenceTargetHealth(
                target=target,
                state=state,
                verification=(
                    InferenceVerification.VERIFIED
                    if verified
                    else InferenceVerification.UNVERIFIED
                ),
                required_capabilities=previous.required_capabilities,
                error_category=category,
                last_checked_at=datetime.now(UTC),
            )

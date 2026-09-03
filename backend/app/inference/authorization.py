from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from app.inference.contracts import InferenceTarget
from app.inference.errors import InferenceError, InferenceErrorCategory


class InferencePrincipalKind(str, Enum):
    CORE = "core"
    ADDON = "addon"


@dataclass(frozen=True)
class InferencePrincipal:
    kind: InferencePrincipalKind
    id: str

    def __post_init__(self) -> None:
        if not self.id.strip() or self.id.strip() != self.id:
            raise ValueError("inference principal id must be canonical")


CORE_TARGET_ALLOWLIST: Mapping[str, frozenset[InferenceTarget]] = {
    "chat": frozenset({InferenceTarget.CHAT}),
    "semantic-privacy": frozenset({InferenceTarget.PRIVACY}),
    "memory-extraction": frozenset({InferenceTarget.MEMORY_EXTRACTION}),
    "memory-consolidation": frozenset({InferenceTarget.MEMORY_CONSOLIDATION}),
    "memory-index": frozenset({InferenceTarget.EMBEDDING}),
    "heavy-reasoning": frozenset({InferenceTarget.HEAVY_REASONING}),
}


class InferenceAuthorizer:
    def __init__(
        self,
        *,
        addon_allowlists: Mapping[str, frozenset[InferenceTarget]] | None = None,
    ) -> None:
        self._addon_allowlists = dict(addon_allowlists or {})

    def authorize(
        self, principal: InferencePrincipal, target: InferenceTarget
    ) -> None:
        allowed = (
            CORE_TARGET_ALLOWLIST.get(principal.id, frozenset())
            if principal.kind is InferencePrincipalKind.CORE
            else self._addon_allowlists.get(principal.id, frozenset())
        )
        if target not in allowed:
            raise InferenceError(
                InferenceErrorCategory.ACCESS_DENIED,
                retryable=False,
            )

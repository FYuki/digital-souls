from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from app.memory.admission.contracts import (
    ApprovedMemoryCandidate,
    MemoryType,
    StructuredValue,
)
from app.privacy.semantic.contracts import PrivacyAssessment


class ConsolidationPlanType(str, Enum):
    KEEP = "KEEP"
    MERGE = "MERGE"
    SUPERSEDE = "SUPERSEDE"
    DELETE_EXACT_DUPLICATE = "DELETE_EXACT_DUPLICATE"
    CONFLICT = "CONFLICT"
    NOOP = "NOOP"


@dataclass(frozen=True)
class MemoryVersionRef:
    memory_id: UUID
    content_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.memory_id, UUID) or self.memory_id.version != 4:
            raise ValueError("memory_id must be a UUID4")
        if (
            isinstance(self.content_version, bool)
            or not isinstance(self.content_version, int)
            or self.content_version < 1
        ):
            raise ValueError("content_version must be a positive integer")


@dataclass(frozen=True)
class ConsolidationPlan:
    plan_type: ConsolidationPlanType
    reason_code: str
    memories: tuple[MemoryVersionRef, ...]
    memory_type: MemoryType | None = None
    structured_value: StructuredValue | None = None
    canonical_memory_id: UUID | None = None


@dataclass(frozen=True)
class ConsolidationResponse:
    plans: tuple[ConsolidationPlan, ...]


@dataclass(frozen=True)
class ConsolidationPrivacyReview:
    candidate: ApprovedMemoryCandidate | None
    assessment: PrivacyAssessment

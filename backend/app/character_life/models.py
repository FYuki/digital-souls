from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    AwareDatetime,
    StringConstraints,
    model_validator,
)

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")]
Content = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]


def now() -> datetime:
    return datetime.now(UTC)


class Result(StrEnum):
    APPLIED = "APPLIED"
    NO_CHANGE = "NO_CHANGE"
    DEFERRED = "DEFERRED"
    SUPERSEDED = "SUPERSEDED"
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"


class Kind(StrEnum):
    INTEREST = "INTEREST"
    ONGOING_ACTIVITY = "ONGOING_ACTIVITY"
    GOAL_INTENTION = "GOAL_INTENTION"
    IMPLEMENTATION_INTENTION = "IMPLEMENTATION_INTENTION"
    NEXT_ACTION_CANDIDATE = "NEXT_ACTION_CANDIDATE"
    SHARE_CANDIDATE = "SHARE_CANDIDATE"


class StateStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DORMANT = "DORMANT"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"
    SUPERSEDED = "SUPERSEDED"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LifeState(Record):
    id: UUID = Field(default_factory=uuid4)
    character_id: Identifier
    kind: Kind
    status: StateStatus = StateStatus.ACTIVE
    content: Content
    target_id: Identifier | None = None
    binding_target_id: Identifier | None = None
    source: Literal["user", "reflection", "activity"]
    source_ids: tuple[UUID, ...] = ()
    reflection_revisions: dict[UUID, str] = Field(default_factory=dict)
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=now)
    updated_at: AwareDatetime = Field(default_factory=now)

    @model_validator(mode="after")
    def consistent(self) -> LifeState:
        if not self.content.strip():
            raise ValueError("empty life state")
        if self.source != "user" and not self.source_ids:
            raise ValueError("derived state requires provenance")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("duplicate source")
        if self.reflection_revisions and (
            self.source != "reflection"
            or set(self.reflection_revisions) != set(self.source_ids)
        ):
            raise ValueError("reflection revision boundary invalid")
        if self.updated_at < self.created_at:
            raise ValueError("invalid state timestamps")
        return self


class Grant(Record):
    character_id: Identifier
    connection_id: Identifier
    connection_identity: str
    enabled: bool
    revision: int = Field(default=1, ge=1)
    updated_at: AwareDatetime = Field(default_factory=now)


class ObservationHandoff(Record):
    """#100へ同じ入力を再送するための承認済み作業記録。SELF Episode正本ではない。"""

    topic: Content
    experienced_at: AwareDatetime = Field(default_factory=now)
    source_revisions: tuple[str, ...]


class Run(Record):
    id: UUID = Field(default_factory=uuid4)
    character_id: Identifier
    state_id: UUID
    state_revision: int
    grant_revision: int
    request_id: str
    requested: bool = False
    phase: Literal["queued", "running", "paused", "finished"] = "queued"
    result: Result | None = None
    reason: str = "queued"
    workflow_id: str
    attempt: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=now)
    finished_at: AwareDatetime | None = None
    source_revisions: tuple[str, ...] = ()
    dependency_results: dict[str, str] = Field(default_factory=dict)
    handoff: ObservationHandoff | None = Field(default=None, repr=False)


class LifeError(Exception):
    """外部本文や秘密を例外へ含めない。"""

    def __init__(self, result: Result, reason: str) -> None:
        self.result, self.reason = result, reason
        super().__init__(reason)


class ReflectionView(Record):
    """#100の正本を参照する読取projection。Reflection本体の永続modelではない。"""

    id: UUID
    character_id: Identifier
    revision: str
    content: Content
    active: bool

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID


class ReflectionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    INACTIVE = "INACTIVE"


class LifeStateKind(str, Enum):
    INTEREST = "INTEREST"
    ONGOING_ACTIVITY = "ONGOING_ACTIVITY"
    GOAL_INTENTION = "GOAL_INTENTION"
    IMPLEMENTATION_INTENTION = "IMPLEMENTATION_INTENTION"
    NEXT_ACTION_CANDIDATE = "NEXT_ACTION_CANDIDATE"
    SHARE_CANDIDATE = "SHARE_CANDIDATE"


class LifeStateStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DORMANT = "DORMANT"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"
    SUPERSEDED = "SUPERSEDED"


class CharacterLifeResult(str, Enum):
    APPLIED = "APPLIED"
    NO_CHANGE = "NO_CHANGE"
    DEFERRED = "DEFERRED"
    SUPERSEDED = "SUPERSEDED"
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"


class BigFiveAspect(str, Enum):
    VOLATILITY = "volatility"
    WITHDRAWAL = "withdrawal"
    COMPASSION = "compassion"
    POLITENESS = "politeness"
    INDUSTRIOUSNESS = "industriousness"
    ORDERLINESS = "orderliness"
    ENTHUSIASM = "enthusiasm"
    ASSERTIVENESS = "assertiveness"
    OPENNESS = "openness"
    INTELLECT = "intellect"


class RelationshipAxis(str, Enum):
    AFFECTIVE_VALENCE = "affective_valence"
    RELATIONAL_PROXIMITY = "relational_proximity"


@dataclass(frozen=True)
class ReflectionRecord:
    id: UUID
    character_id: str
    content: str
    status: ReflectionStatus
    source_episode_ids: tuple[UUID, ...]
    model_id: str
    prompt_version: str
    policy_version: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_uuid4(self.id, "id")
        _require_non_empty(self.character_id, "character_id")
        _require_non_empty(self.content, "content")
        if not isinstance(self.status, ReflectionStatus):
            raise TypeError("status must be a ReflectionStatus")
        if not self.source_episode_ids:
            raise ValueError("source_episode_ids must not be empty")
        if len(set(self.source_episode_ids)) != len(self.source_episode_ids):
            raise ValueError("source_episode_ids must not contain duplicates")
        for episode_id in self.source_episode_ids:
            _require_uuid4(episode_id, "source_episode_ids")
        for field_name in ("model_id", "prompt_version", "policy_version"):
            _require_non_empty(getattr(self, field_name), field_name)
        _require_aware_datetime(self.created_at, "created_at")
        _require_aware_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")


@dataclass(frozen=True)
class LifeStateRecord:
    id: UUID
    character_id: str
    kind: LifeStateKind
    status: LifeStateStatus
    content: str
    source_reflection_ids: tuple[UUID, ...]
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_uuid4(self.id, "id")
        _require_non_empty(self.character_id, "character_id")
        if not isinstance(self.kind, LifeStateKind):
            raise TypeError("kind must be a LifeStateKind")
        if not isinstance(self.status, LifeStateStatus):
            raise TypeError("status must be a LifeStateStatus")
        _require_non_empty(self.content, "content")
        if len(set(self.source_reflection_ids)) != len(self.source_reflection_ids):
            raise ValueError("source_reflection_ids must not contain duplicates")
        for reflection_id in self.source_reflection_ids:
            _require_uuid4(reflection_id, "source_reflection_ids")
        _require_aware_datetime(self.created_at, "created_at")
        _require_aware_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")


@dataclass(frozen=True)
class SelfEpisodeInput:
    character_id: str
    event_type: str
    topic: str
    experienced_at: datetime
    source_provider_id: str
    source_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.character_id, "character_id")
        if self.event_type not in {"OBSERVATION", "ACTIVITY", "ENCOUNTER"}:
            raise ValueError("event_type must be a SELF episode event type")
        _require_non_empty(self.topic, "topic")
        _require_aware_datetime(self.experienced_at, "experienced_at")
        _require_non_empty(self.source_provider_id, "source_provider_id")
        _require_non_empty(self.source_ref, "source_ref")


@dataclass(frozen=True)
class CharacterLifeSliceInput:
    character_id: str
    episode_ids: tuple[UUID, ...]
    requested_at: datetime

    def __post_init__(self) -> None:
        _require_non_empty(self.character_id, "character_id")
        if len(self.episode_ids) < 2:
            raise ValueError("episode_ids must contain at least two episodes")
        if len(set(self.episode_ids)) != len(self.episode_ids):
            raise ValueError("episode_ids must not contain duplicates")
        for episode_id in self.episode_ids:
            _require_uuid4(episode_id, "episode_ids")
        _require_aware_datetime(self.requested_at, "requested_at")


def _require_uuid4(value: object, field_name: str) -> None:
    if not isinstance(value, UUID) or value.version != 4:
        raise ValueError(f"{field_name} must be a UUID4")


def _require_non_empty(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_aware_datetime(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name} must be timezone-aware")

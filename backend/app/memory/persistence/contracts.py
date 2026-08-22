from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.memory.admission.contracts import MemoryType, StructuredValue


class FormationMethod(str, Enum):
    DIRECT = "DIRECT"
    EXTRACTED = "EXTRACTED"
    ADDON_EVENT = "ADDON_EVENT"
    CONSOLIDATED = "CONSOLIDATED"


class TemporalPrecision(str, Enum):
    YEAR = "YEAR"
    MONTH = "MONTH"
    DAY = "DAY"
    HOUR = "HOUR"
    MINUTE = "MINUTE"
    SECOND = "SECOND"


class MemoryStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class MemorySourceType(str, Enum):
    CONVERSATION_TURN = "CONVERSATION_TURN"
    PROVIDER_RECORD = "PROVIDER_RECORD"
    ADDON_EVENT = "ADDON_EVENT"


class MemoryLineageRelation(str, Enum):
    CONSOLIDATED_FROM = "CONSOLIDATED_FROM"
    SUPERSEDES = "SUPERSEDES"
    DUPLICATE_OF = "DUPLICATE_OF"


@dataclass(frozen=True)
class MemorySourceInput:
    source_type: MemorySourceType
    source_provider_id: str
    source_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_type, MemorySourceType):
            raise TypeError("source_type must be a MemorySourceType")
        _require_non_empty(self.source_provider_id, "source_provider_id")
        _require_non_empty(self.source_ref, "source_ref")


@dataclass(frozen=True)
class MemoryLineageInput:
    related_memory_id: UUID
    relation: MemoryLineageRelation

    def __post_init__(self) -> None:
        if (
            not isinstance(self.related_memory_id, UUID)
            or self.related_memory_id.version != 4
        ):
            raise ValueError("related_memory_id must be a UUID4")
        if not isinstance(self.relation, MemoryLineageRelation):
            raise TypeError("relation must be a MemoryLineageRelation")


@dataclass(frozen=True)
class MemoryWriteContext:
    formation_method: FormationMethod
    idempotency_key: str
    occurred_at: datetime | None
    occurred_timezone: str | None
    occurred_precision: TemporalPrecision | None
    stated_at: datetime
    expires_at: datetime | None
    policy_version: str
    classifier_version: str
    model_id: str
    model_digest: str
    prompt_version: str
    sources: tuple[MemorySourceInput, ...]
    lineage: tuple[MemoryLineageInput, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.formation_method, FormationMethod):
            raise TypeError("formation_method must be a FormationMethod")
        occurred_values = (
            self.occurred_at,
            self.occurred_timezone,
            self.occurred_precision,
        )
        if any(value is None for value in occurred_values) and not all(
            value is None for value in occurred_values
        ):
            raise ValueError("occurred date fields must be all known or all unknown")
        if self.occurred_at is not None:
            _require_aware_datetime(self.occurred_at, "occurred_at")
            if not isinstance(self.occurred_precision, TemporalPrecision):
                raise TypeError("occurred_precision must be a TemporalPrecision")
            assert self.occurred_timezone is not None
            _require_non_empty(self.occurred_timezone, "occurred_timezone")
            try:
                ZoneInfo(self.occurred_timezone)
            except ZoneInfoNotFoundError as error:
                raise ValueError(
                    "occurred_timezone must be an IANA timezone"
                ) from error
        _require_aware_datetime(self.stated_at, "stated_at")
        if self.expires_at is not None:
            _require_aware_datetime(self.expires_at, "expires_at")
        _require_non_empty(self.idempotency_key, "idempotency_key")
        for field_name in (
            "policy_version",
            "classifier_version",
            "model_id",
            "model_digest",
            "prompt_version",
        ):
            _require_non_empty(getattr(self, field_name), field_name)
        if not self.sources:
            raise ValueError("sources must contain at least one typed source")
        if any(not isinstance(source, MemorySourceInput) for source in self.sources):
            raise TypeError("sources must contain only MemorySourceInput values")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources must not contain duplicates")
        if any(not isinstance(item, MemoryLineageInput) for item in self.lineage):
            raise TypeError("lineage must contain only MemoryLineageInput values")
        if len(set(self.lineage)) != len(self.lineage):
            raise ValueError("lineage must not contain duplicates")


@dataclass(frozen=True)
class ApprovedMemory:
    id: UUID
    character_id: str
    provider_id: str
    memory_kind: str
    memory_type: MemoryType
    structured_value: StructuredValue
    normalized_text: str
    policy_version: str
    content_version: int
    status: MemoryStatus
    occurred_at: datetime | None
    occurred_timezone: str | None
    occurred_precision: TemporalPrecision | None
    stated_at: datetime
    expires_at: datetime | None
    last_user_mentioned_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TemporaryProviderRecordInput:
    provider_id: str
    source_ref: str
    record_type: str
    structured_value: str
    effective_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "provider_id",
            "source_ref",
            "record_type",
            "structured_value",
        ):
            _require_non_empty(getattr(self, field_name), field_name)
        _require_aware_datetime(self.effective_at, "effective_at")


@dataclass(frozen=True)
class TemporaryProviderRecord:
    id: UUID
    character_id: str
    provider_id: str
    source_ref: str
    record_type: str
    structured_value: str
    effective_at: datetime
    created_at: datetime
    updated_at: datetime


def build_conversation_idempotency_key(
    *,
    character_id: str,
    conversation_id: str,
    turn_id: str,
    candidate_index: int,
    extractor_version: str,
) -> str:
    for value, field_name in (
        (character_id, "character_id"),
        (conversation_id, "conversation_id"),
        (turn_id, "turn_id"),
        (extractor_version, "extractor_version"),
    ):
        _require_non_empty(value, field_name)
        if ":" in value:
            raise ValueError(f"{field_name} must not contain ':'")
    if (
        isinstance(candidate_index, bool)
        or not isinstance(candidate_index, int)
        or candidate_index < 0
    ):
        raise ValueError("candidate_index must be a non-negative integer")
    return ":".join(
        (
            character_id,
            conversation_id,
            turn_id,
            str(candidate_index),
            extractor_version,
        )
    )


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_non_empty(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")

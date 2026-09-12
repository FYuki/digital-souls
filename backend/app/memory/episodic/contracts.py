"""経験と取得情報を分離する型。未知の5Wを必須値で埋めない。"""

from __future__ import annotations

import calendar
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator

Text = Annotated[str, Field(strict=True, min_length=1, max_length=240)]
Identity = Annotated[str, Field(strict=True, min_length=1, max_length=160)]
Version = Annotated[int, Field(strict=True, ge=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def clean_strings(cls, value: object) -> object:
        if isinstance(value, str) and (
            value != value.strip() or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("text must be trimmed and single-line")
        return value


class NarrativeContext(str, Enum):
    REPORTED = "REPORTED"
    HYPOTHETICAL = "HYPOTHETICAL"
    FICTIONAL = "FICTIONAL"
    UNKNOWN = "UNKNOWN"


class PersonRole(str, Enum):
    ACTOR = "ACTOR"
    PARTICIPANT = "PARTICIPANT"
    SPEAKER = "SPEAKER"
    LISTENER = "LISTENER"
    TOPIC = "TOPIC"


class Person(Contract):
    name: Text
    role: PersonRole
    # 抽出時はアプリケーションが提示した人物IDだけを許可する。
    entity_id: Identity | None = None


class What(Contract):
    predicate: Text
    object: Text | None = None
    polarity: Literal["AFFIRMED", "NEGATED", "UNKNOWN"] = "UNKNOWN"
    actuality: Literal["OCCURRED", "PLANNED", "CONDITIONAL", "UNKNOWN"] = "UNKNOWN"


class Place(Contract):
    name: Text
    entity_id: Identity | None = None


class TimeParts(Contract):
    year: Annotated[int, Field(strict=True, ge=1, le=9999)] | None = None
    month: Annotated[int, Field(strict=True, ge=1, le=12)] | None = None
    day: Annotated[int, Field(strict=True, ge=1, le=31)] | None = None
    hour: Annotated[int, Field(strict=True, ge=0, le=23)] | None = None
    minute: Annotated[int, Field(strict=True, ge=0, le=59)] | None = None
    second: Annotated[int, Field(strict=True, ge=0, le=59)] | None = None

    @model_validator(mode="after")
    def valid_calendar(self) -> Self:
        if self.month is not None and self.day is not None:
            if self.day > calendar.monthrange(self.year or 2000, self.month)[1]:
                raise ValueError("invalid calendar date")
        return self

    @property
    def precision(self) -> str:
        values = (self.year, self.month, self.day, self.hour, self.minute, self.second)
        if all(value is None for value in values):
            return "UNKNOWN"
        count = 0
        while count < len(values) and values[count] is not None:
            count += 1
        if any(value is not None for value in values[count:]):
            return "PARTIAL"
        return ("YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND")[count - 1]


class TimeExpression(Contract):
    """LLM出力。timezoneと処理時刻を生成させない。"""

    parts: TimeParts = Field(default_factory=TimeParts)
    end: TimeParts | None = None
    range_kind: Literal["POINT", "UNCERTAINTY", "DURATION"] = "POINT"
    relative_unit: Literal["DAY", "MONTH", "YEAR"] | None = None
    relative_offset: Annotated[int, Field(strict=True, ge=-12000, le=12000)] | None = None

    @model_validator(mode="after")
    def valid_expression(self) -> Self:
        if (self.relative_unit is None) != (self.relative_offset is None):
            raise ValueError("relative time requires unit and offset")
        if self.relative_unit is not None:
            if self.parts.precision != "UNKNOWN" or self.end is not None:
                raise ValueError("relative and absolute dates must not be mixed")
        if (self.end is None) != (self.range_kind == "POINT"):
            raise ValueError("time ranges require an end and a range kind")
        return self


class ResolvedTime(Contract):
    parts: TimeParts = Field(default_factory=TimeParts)
    end: TimeParts | None = None
    range_kind: Literal["POINT", "UNCERTAINTY", "DURATION"] = "POINT"
    timezone: Identity
    reference_at: datetime

    @field_validator("reference_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reference_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def valid_time(self) -> Self:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("invalid timezone") from exc
        if (self.end is None) != (self.range_kind == "POINT"):
            raise ValueError("invalid time range")
        if self.end is not None:
            names = ("year", "month", "day", "hour", "minute", "second")
            start = tuple(getattr(self.parts, name) for name in names)
            end = tuple(getattr(self.end, name) for name in names)
            if self.parts.precision == self.end.precision and self.parts.precision not in {
                "UNKNOWN", "PARTIAL"
            }:
                left = tuple(value for value in start if value is not None)
                right = tuple(value for value in end if value is not None)
                if left > right:
                    raise ValueError("time range is reversed")
        return self


class FiveW(Contract):
    who: Annotated[tuple[Person, ...], Field(max_length=16)] = ()
    what: What
    when: ResolvedTime | None = None
    where: Place | None = None
    why: Text | None = None
    context: NarrativeContext = NarrativeContext.UNKNOWN

    @model_validator(mode="after")
    def unique_people(self) -> Self:
        identities = [(person.entity_id, person.role) for person in self.who if person.entity_id]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate identified participant role")
        return self


class ExtractedFiveW(Contract):
    who: Annotated[tuple[Person, ...], Field(max_length=16)] = ()
    what: What
    when: TimeExpression | None = None
    where: Place | None = None
    why: Text | None = None
    context: NarrativeContext = NarrativeContext.UNKNOWN


class SourceSpan(Contract):
    source_id: UUID4
    revision: Version
    role: Literal["user", "assistant", "activity", "manual"]
    start: Annotated[int, Field(strict=True, ge=0)]
    end: Annotated[int, Field(strict=True, ge=1)]
    stated_at: datetime

    @field_validator("stated_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if self.end <= self.start:
            raise ValueError("source range must not be empty")
        return self

    @property
    def identity(self) -> tuple[UUID, int, str, int, int]:
        return self.source_id, self.revision, self.role, self.start, self.end


class RecordStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    DELETED = "DELETED"


class RecordKind(str, Enum):
    EPISODE = "EPISODE"
    FACT = "FACT"


class FormationStamp(Contract):
    policy_version: Identity
    classifier_version: Identity
    model_id: Identity
    model_digest: Identity
    prompt_version: Identity


class Record(Contract):
    id: UUID4
    kind: RecordKind
    character_id: Identity
    conversation_id: UUID4 | None
    content_version: Version
    status: RecordStatus
    five_w: FiveW | None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def valid_content(self) -> Self:
        if (self.status is RecordStatus.DELETED) != (self.five_w is None):
            raise ValueError("only deleted records must omit content")
        return self


class Reference(Contract):
    id: UUID4
    character_id: Identity
    episode_id: UUID4
    episode_version: Version
    fact_id: UUID4
    fact_version: Version
    sources: Annotated[tuple[SourceSpan, ...], Field(min_length=1)]
    valid: bool


class MergeRelation(Contract):
    id: UUID4
    character_id: Identity
    source_fact_id: UUID4
    source_version: Version
    target_fact_id: UUID4
    target_version: Version
    conversation_id: UUID4
    policy: Identity
    evidence: Annotated[tuple[SourceSpan, ...], Field(min_length=1)]
    valid: bool

    @model_validator(mode="after")
    def different_records(self) -> Self:
        if self.source_fact_id == self.target_fact_id:
            raise ValueError("a Fact cannot merge into itself")
        return self


def text_slots(value: FiveW) -> tuple[str, ...]:
    """IDも含め、保存される自由文字列を漏れなくprivacy検査へ渡す。"""
    slots = [value.what.predicate]
    if value.what.object is not None:
        slots.append(value.what.object)
    if value.why is not None:
        slots.append(value.why)
    for person in value.who:
        slots.append(person.name)
        if person.entity_id is not None:
            slots.append(person.entity_id)
    if value.where is not None:
        slots.append(value.where.name)
        if value.where.entity_id is not None:
            slots.append(value.where.entity_id)
    return tuple(slots)

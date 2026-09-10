"""所有キャラクターの視点で表すEpisodeの共通契約。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class EpisodicEventType(str, Enum):
    SHARED_MILESTONE = "SHARED_MILESTONE"
    ACHIEVEMENT = "ACHIEVEMENT"
    DECISION = "DECISION"
    OUTCOME = "OUTCOME"
    CHANGE = "CHANGE"
    OBSERVATION = "OBSERVATION"
    ACTIVITY = "ACTIVITY"
    ENCOUNTER = "ENCOUNTER"


class TemporalPrecision(str, Enum):
    YEAR = "YEAR"
    MONTH = "MONTH"
    DAY = "DAY"
    HOUR = "HOUR"
    MINUTE = "MINUTE"
    SECOND = "SECOND"


class ParticipantRole(str, Enum):
    COMPANION = "COMPANION"
    SPEAKER = "SPEAKER"
    LISTENER = "LISTENER"
    SUBJECT = "SUBJECT"


def short_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 60
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(f"{field} must contain 1 to 60 single-line characters")
    return value


def aware_datetime(value: datetime, field: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be timezone-aware")


def validate_occurrence(
    at: datetime | None, timezone: str | None, precision: TemporalPrecision | None
) -> None:
    fields = (at, timezone, precision)
    if all(value is None for value in fields):
        return
    if any(value is None for value in fields):
        raise ValueError("occurred date fields must be all known or all unknown")
    assert at is not None and timezone is not None
    aware_datetime(at, "occurred_at")
    if not isinstance(precision, TemporalPrecision):
        raise TypeError("occurred_precision must be a TemporalPrecision")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise ValueError("occurred_timezone must be an IANA timezone") from error


@dataclass(frozen=True)
class EpisodeParticipant:
    name: str
    role: ParticipantRole
    entity_id: str | None = None
    entity_namespace: str | None = None

    def __post_init__(self) -> None:
        short_text(self.name, "name")
        if not isinstance(self.role, ParticipantRole):
            raise TypeError("role must be a ParticipantRole")
        if (self.entity_id is None) != (self.entity_namespace is None):
            raise ValueError("entity identity requires both namespace and id")
        if self.entity_id is not None:
            short_text(self.entity_id, "entity_id")
            short_text(self.entity_namespace, "entity_namespace")

    @property
    def identity(self) -> tuple[str, str] | None:
        # 名称だけの一致は本人同定の証拠にしない。
        if self.entity_id is None or self.entity_namespace is None:
            return None
        return self.entity_namespace, self.entity_id


def _participants(value: tuple[EpisodeParticipant, ...], *, related: bool) -> None:
    if not isinstance(value, tuple) or len(value) > 16:
        raise ValueError("participants must be a tuple of at most 16 people")
    for person in value:
        if not isinstance(person, EpisodeParticipant):
            raise TypeError("participants must contain EpisodeParticipant values")
        if (person.role is ParticipantRole.SUBJECT) != related:
            raise ValueError(
                "topic subjects and experience participants must be separate"
            )
    identified = [(person.identity, person.role) for person in value if person.identity]
    if len(set(identified)) != len(identified):
        raise ValueError("participant identity and role must not be duplicated")


@dataclass(frozen=True)
class RelatedEpisode:
    description: str
    participants: tuple[EpisodeParticipant, ...] = ()
    memory_id: UUID | None = None
    occurred_at: datetime | None = None
    occurred_timezone: str | None = None
    occurred_precision: TemporalPrecision | None = None

    def __post_init__(self) -> None:
        short_text(self.description, "description")
        _participants(self.participants, related=True)
        if self.memory_id is not None and (
            not isinstance(self.memory_id, UUID) or self.memory_id.version != 4
        ):
            raise ValueError("related memory_id must be a UUID4")
        validate_occurrence(
            self.occurred_at, self.occurred_timezone, self.occurred_precision
        )


@dataclass(frozen=True)
class EpisodicEventValue:
    event_type: EpisodicEventType
    character_id: str
    character_name: str
    topic: str
    action: str
    participants: tuple[EpisodeParticipant, ...] = ()
    related_event: RelatedEpisode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, EpisodicEventType):
            raise TypeError("event_type must be an EpisodicEventType")
        for field in ("character_id", "character_name", "topic", "action"):
            short_text(getattr(self, field), field)
        _participants(self.participants, related=False)
        if any(
            p.identity == ("character", self.character_id) for p in self.participants
        ):
            raise ValueError(
                "the owner must not be repeated as an experience participant"
            )
        if self.related_event is not None and not isinstance(
            self.related_event, RelatedEpisode
        ):
            raise TypeError("related_event must be a RelatedEpisode")


def _object(
    value: object, required: set[str], optional: set[str]
) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or not required <= value.keys() <= required | optional
    ):
        raise ValueError("invalid Episode fields")
    return value


def parse_participant(value: object) -> EpisodeParticipant:
    raw = _object(value, {"name", "role"}, {"entity_id", "entity_namespace"})
    return EpisodeParticipant(
        name=short_text(raw["name"], "name"),
        role=ParticipantRole(raw["role"]),
        entity_id=None
        if raw.get("entity_id") is None
        else short_text(raw["entity_id"], "entity_id"),
        entity_namespace=None
        if raw.get("entity_namespace") is None
        else short_text(raw["entity_namespace"], "entity_namespace"),
    )


def parse_participants(value: object) -> tuple[EpisodeParticipant, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("participants must be an array")
    return tuple(parse_participant(item) for item in value)


def parse_related_episode(value: object) -> RelatedEpisode | None:
    if value is None:
        return None
    raw = _object(
        value,
        {"description"},
        {
            "participants",
            "memory_id",
            "occurred_at",
            "occurred_timezone",
            "occurred_precision",
        },
    )
    at = raw.get("occurred_at")
    if at is not None and not isinstance(at, str):
        raise ValueError("related occurred_at must be an ISO timestamp")
    memory_id = raw.get("memory_id")
    if memory_id is not None and not isinstance(memory_id, str):
        raise ValueError("related memory_id must be a UUID string")
    return RelatedEpisode(
        description=short_text(raw["description"], "description"),
        participants=parse_participants(raw.get("participants", [])),
        memory_id=None if memory_id is None else UUID(memory_id),
        occurred_at=None if at is None else datetime.fromisoformat(at),
        occurred_timezone=None
        if raw.get("occurred_timezone") is None
        else short_text(raw["occurred_timezone"], "occurred_timezone"),
        occurred_precision=None
        if raw.get("occurred_precision") is None
        else TemporalPrecision(raw["occurred_precision"]),
    )


def parse_episode_value(value: object) -> EpisodicEventValue:
    raw = _object(
        value,
        {"event_type", "character_id", "character_name", "topic", "action"},
        {"participants", "related_event"},
    )
    return EpisodicEventValue(
        event_type=EpisodicEventType(raw["event_type"]),
        character_id=short_text(raw["character_id"], "character_id"),
        character_name=short_text(raw["character_name"], "character_name"),
        topic=short_text(raw["topic"], "topic"),
        action=short_text(raw["action"], "action"),
        participants=parse_participants(raw.get("participants", [])),
        related_event=parse_related_episode(raw.get("related_event")),
    )


def episode_slots(value: EpisodicEventValue) -> dict[str, str]:
    result = {
        field: getattr(value, field)
        for field in ("character_id", "character_name", "topic", "action")
    }
    groups = [("participants", value.participants)]
    if value.related_event is not None:
        result["related_event.description"] = value.related_event.description
        groups.append(("related_event.participants", value.related_event.participants))
    for prefix, people in groups:
        for index, person in enumerate(people):
            for field in ("name", "entity_id", "entity_namespace"):
                text = getattr(person, field)
                if text is not None:
                    result[f"{prefix}.{index}.{field}"] = text
    return result


def render_episode(value: EpisodicEventValue) -> str:
    clauses: list[str] = []
    for role, particle in (
        (ParticipantRole.SPEAKER, "から"),
        (ParticipantRole.COMPANION, "と"),
        (ParticipantRole.LISTENER, "に"),
    ):
        names = [person.name for person in value.participants if person.role is role]
        if names:
            clauses.append("、".join(names) + particle)
    related = value.related_event
    if related is not None:
        related_names = "、".join(person.name for person in related.participants)
        when = render_occurrence(
            related.occurred_at, related.occurred_timezone, related.occurred_precision
        )
        clauses.append(
            (related_names + "が" if related_names else "")
            + (when + "に" if when else "")
            + related.description.rstrip("。")
            + "ことについて"
        )
    prefix = "、".join(clauses)
    return (
        value.character_name
        + "は"
        + (prefix + "、" if prefix else "")
        + value.action.rstrip("。")
        + "。"
    )


def render_occurrence(
    at: datetime | None, timezone: str | None, precision: TemporalPrecision | None
) -> str:
    """判明した精度だけを表示し、月初や午前零時を実際の日時に見せない。"""
    validate_occurrence(at, timezone, precision)
    if at is None:
        return ""
    assert timezone is not None and precision is not None
    formats = {
        TemporalPrecision.YEAR: "%Y年",
        TemporalPrecision.MONTH: "%Y年%m月",
        TemporalPrecision.DAY: "%Y年%m月%d日",
        TemporalPrecision.HOUR: "%Y年%m月%d日%H時",
        TemporalPrecision.MINUTE: "%Y年%m月%d日%H時%M分",
        TemporalPrecision.SECOND: "%Y年%m月%d日%H時%M分%S秒",
    }
    text = at.astimezone(ZoneInfo(timezone)).strftime(formats[precision])
    if precision in {
        TemporalPrecision.HOUR,
        TemporalPrecision.MINUTE,
        TemporalPrecision.SECOND,
    }:
        text += f"（{timezone}）"
    return text

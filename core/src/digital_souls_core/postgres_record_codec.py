"""Explicit typed canonical record encoding, confined to the persistence adapter."""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .application import CoreError
from .history import Binding, SourceReference
from .memory_contracts import SourceVersion
from .memory_record_store import MemoryRecord
from .memory_records import (
    Citation,
    Episode,
    EpisodeFactLink,
    ExplicitReason,
    Fact,
    FiveW,
    PartialDateTime,
    RecordKind,
    RecordRef,
    Semantic,
    Speaker,
    TemporalValue,
    TimePrecision,
)


def encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return encode(asdict(value))
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [encode(v) for v in value]
    if isinstance(value, (set, frozenset)):
        # Collections represent sets in the registration contract, not chronology.
        return sorted((encode(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def normalized(value: Any) -> Any:
    """Deduplicate set-valued citations/evidence and order all registration collections."""
    encoded = encode(value)

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        if isinstance(item, list):
            unique = {
                json.dumps(walk(v), sort_keys=True, separators=(",", ":")): walk(v) for v in item
            }
            return [unique[k] for k in sorted(unique)]
        return item

    return walk(encoded)


def citation(raw: dict[str, Any], binding: Binding) -> Citation:
    source = raw["source"]
    return Citation(
        binding,
        SourceVersion(SourceReference(**source["reference"]), source["epoch"]),
        Speaker(raw["speaker"]),
        raw["start"],
        raw["end"],
    )


def temporal(raw: dict[str, Any]) -> TemporalValue:
    def partial(value: dict[str, Any] | None) -> PartialDateTime | None:
        return (
            None
            if value is None
            else PartialDateTime(**{**value, "precision": TimePrecision(value["precision"])})
        )

    return TemporalValue(
        start=partial(raw["start"]), end=partial(raw["end"]), timezone=raw["timezone"]
    )


def five_w(raw: dict[str, Any], binding: Binding) -> FiveW:
    why = raw["why"]
    return FiveW(
        **{
            **raw,
            "when": None if raw["when"] is None else temporal(raw["when"]),
            "why": None
            if why is None
            else ExplicitReason(why["text"], tuple(citation(c, binding) for c in why["citations"])),
        }
    )


def projection(value: TemporalValue) -> tuple[datetime | None, datetime | None, str | None]:
    try:
        zone = ZoneInfo(value.timezone) if value.timezone is not None else None
        if value.start is None:
            return None, None, None
        precision = value.start.precision
        if zone is None:
            return None, None, precision.value

        def beginning(v: PartialDateTime) -> datetime:
            return datetime(
                v.year,
                v.month or 1,
                v.day or 1,
                v.hour or 0,
                v.minute or 0,
                v.second or 0,
                tzinfo=zone,
            )

        start = beginning(value.start)
        last = beginning(value.end or value.start)
        if precision is TimePrecision.YEAR:
            end = last.replace(year=last.year + 1)
        elif precision is TimePrecision.MONTH:
            end = last.replace(year=last.year + (last.month == 12), month=last.month % 12 + 1)
        else:
            end = (
                last
                + {
                    "day": timedelta(days=1),
                    "hour": timedelta(hours=1),
                    "minute": timedelta(minutes=1),
                    "second": timedelta(seconds=1),
                }[precision.value]
            )
        if start.astimezone(UTC) >= end.astimezone(UTC):
            raise ValueError("invalid temporal interval")
        return start.astimezone(UTC), end.astimezone(UTC), precision.value
    except (ZoneInfoNotFoundError, ValueError, OverflowError):
        raise CoreError(409, "memory_time_invalid", "Memory time is unavailable") from None


def ref(record: MemoryRecord) -> RecordRef:
    if isinstance(record, Episode):
        kind, identifier = RecordKind.EPISODE, record.episode_id
    elif isinstance(record, Fact):
        kind, identifier = RecordKind.FACT, record.fact_id
    elif isinstance(record, Semantic):
        kind, identifier = RecordKind.SEMANTIC, record.semantic_id
    else:
        kind, identifier = RecordKind.EPISODE_FACT_LINK, record.link_id
    return RecordRef(
        kind=kind, record_id=identifier, version=record.version, binding=record.binding
    )


def record_citations(record: MemoryRecord) -> tuple[Citation, ...]:
    if isinstance(record, EpisodeFactLink):
        return ()
    values = record.citations
    if isinstance(record, (Episode, Fact)) and record.five_w.why is not None:
        values += record.five_w.why.citations
    return tuple(set(values))

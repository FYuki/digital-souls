from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.memory.persistence.contracts import TemporalPrecision


class DateExpressionRole(str, Enum):
    PRIMARY = "PRIMARY"
    START = "START"
    END = "END"


@dataclass(frozen=True)
class AbsoluteDateExpression:
    role: DateExpressionRole
    year: int
    month: int | None = None
    day: int | None = None


@dataclass(frozen=True)
class RelativeDateExpression:
    role: DateExpressionRole
    year_offset: int | None = None
    month_offset: int | None = None
    week_offset: int | None = None
    day_offset: int | None = None
    month: int | None = None
    day: int | None = None
    weekday: int | None = None


DateExpression: TypeAlias = AbsoluteDateExpression | RelativeDateExpression


@dataclass(frozen=True)
class OccurredAtResolution:
    occurred_at: datetime | None
    occurred_timezone: str | None
    occurred_precision: TemporalPrecision | None


def resolve_occurred_at(
    expressions: tuple[DateExpression, ...],
    *,
    stated_at: datetime,
    timezone: str,
) -> OccurredAtResolution:
    if stated_at.tzinfo is None or stated_at.utcoffset() is None:
        raise ValueError("stated_at must be timezone-aware")
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("timezone must be an IANA timezone") from error
    primary_expressions = tuple(
        item for item in expressions if item.role is DateExpressionRole.PRIMARY
    )
    if len(primary_expressions) != 1:
        return OccurredAtResolution(None, None, None)
    primary = primary_expressions[0]
    try:
        local, precision = _resolve_expression(primary, stated_at.astimezone(zone))
    except (OverflowError, ValueError):
        return OccurredAtResolution(None, None, None)
    if not _round_trips_through_utc(local, zone):
        return OccurredAtResolution(None, None, None)
    return OccurredAtResolution(local.astimezone(UTC), timezone, precision)


def _round_trips_through_utc(local: datetime, zone: ZoneInfo) -> bool:
    round_trip = local.astimezone(UTC).astimezone(zone)
    return local.replace(tzinfo=None) == round_trip.replace(tzinfo=None)


def _resolve_expression(
    expression: DateExpression, reference: datetime
) -> tuple[datetime, TemporalPrecision]:
    if isinstance(expression, AbsoluteDateExpression):
        if expression.month is None and expression.day is not None:
            raise ValueError("absolute day requires a month")
        if expression.month is None:
            return reference.replace(
                year=expression.year, month=1, day=1, hour=0, minute=0, second=0,
                microsecond=0,
            ), TemporalPrecision.YEAR
        if expression.day is None:
            return reference.replace(
                year=expression.year, month=expression.month, day=1, hour=0,
                minute=0, second=0, microsecond=0,
            ), TemporalPrecision.MONTH
        return reference.replace(
            year=expression.year, month=expression.month, day=expression.day,
            hour=0, minute=0, second=0, microsecond=0,
        ), TemporalPrecision.DAY

    if all(
        value is None
        for value in (
            expression.year_offset,
            expression.month_offset,
            expression.week_offset,
            expression.day_offset,
            expression.month,
            expression.day,
            expression.weekday,
        )
    ):
        raise ValueError("relative date expression requires a date field")
    year = reference.year + (expression.year_offset or 0)
    month = reference.month
    if expression.month is not None:
        month = expression.month
    month_index = year * 12 + month - 1 + (expression.month_offset or 0)
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(reference.day, calendar.monthrange(year, month)[1])
    if expression.day is not None:
        day = expression.day
    resolved = reference.replace(year=year, month=month, day=day)
    if expression.week_offset is not None or expression.weekday is not None:
        resolved += timedelta(weeks=expression.week_offset or 0)
        if expression.weekday is not None:
            resolved += timedelta(days=expression.weekday - resolved.weekday())
    resolved += timedelta(days=expression.day_offset or 0)
    if (
        expression.week_offset is not None
        or expression.weekday is not None
        or expression.day is not None
        or expression.day_offset is not None
    ):
        return resolved.replace(
            hour=0, minute=0, second=0, microsecond=0
        ), TemporalPrecision.DAY
    if expression.month is not None or expression.month_offset is not None:
        return resolved.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ), TemporalPrecision.MONTH
    if expression.year_offset is not None:
        return resolved.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        ), TemporalPrecision.YEAR
    return resolved, TemporalPrecision.SECOND

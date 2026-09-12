"""日時の精度を保った期間照合。検索境界を発生日として保存・表示しない。"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.memory.episodic.contracts import ResolvedTime, TimeParts


def _bounds(parts: TimeParts, timezone: str) -> tuple[datetime, datetime] | None:
    if parts.precision in {"UNKNOWN", "PARTIAL"}:
        return None
    assert parts.year is not None
    zone = ZoneInfo(timezone)
    lower = datetime(parts.year, parts.month or 1, parts.day or 1,
                     parts.hour or 0, parts.minute or 0, parts.second or 0, tzinfo=zone)
    try:
        if parts.precision == "YEAR":
            upper = lower.replace(year=parts.year + 1)
        elif parts.precision == "MONTH":
            upper = (lower.replace(year=parts.year + 1, month=1)
                     if parts.month == 12 else lower.replace(month=(parts.month or 1) + 1))
        else:
            seconds = {"DAY": 86400, "HOUR": 3600, "MINUTE": 60, "SECOND": 1}
            upper = lower + timedelta(seconds=seconds[parts.precision])
        return lower.astimezone(UTC), upper.astimezone(UTC)
    except (OverflowError, ValueError):
        # 上限年を超える境界は確定できない。日付を丸めて一致させない。
        return None


def matches_time(value: ResolvedTime | None, start: datetime, end: datetime) -> bool:
    if value is None:
        return False
    left = _bounds(value.parts, value.timezone)
    right = _bounds(value.end, value.timezone) if value.end else left
    if left is None or right is None:
        return False
    if value.range_kind == "DURATION":
        # 継続期間の開始が検索期間内でなくても、確実に重なる部分を扱う。
        return left[1] <= end and right[0] >= start
    # 月しか分からない出来事を月内の特定日に起きたと扱わない。
    # 幅のある不確かな日時も、可能な範囲全体が検索期間に収まる場合だけ一致。
    return start <= left[0] and right[1] <= end


def exact_occurred_at(value: ResolvedTime | None) -> datetime | None:
    """既存の索引metadataには秒まで確定した一点だけを渡す。"""
    if value is None or value.end is not None or value.parts.precision != "SECOND":
        return None
    bounds = _bounds(value.parts, value.timezone)
    return None if bounds is None else bounds[0]

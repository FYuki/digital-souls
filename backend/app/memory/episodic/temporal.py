"""相対日時は元発言と設定timezoneで解決し、未知の精度を保持する。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.memory.episodic.contracts import ResolvedTime, TimeExpression, TimeParts


def resolve_time(
    expression: TimeExpression, *, stated_at: datetime, timezone: str
) -> ResolvedTime:
    if stated_at.tzinfo is None or stated_at.utcoffset() is None:
        raise ValueError("source timestamp must be timezone-aware")
    local = stated_at.astimezone(ZoneInfo(timezone))
    parts = expression.parts
    if expression.relative_unit is not None:
        assert expression.relative_offset is not None
        offset = expression.relative_offset
        if expression.relative_unit == "DAY":
            day = local.date() + timedelta(days=offset)
            parts = TimeParts(year=day.year, month=day.month, day=day.day)
        elif expression.relative_unit == "MONTH":
            month_index = local.year * 12 + local.month - 1 + offset
            year, month = divmod(month_index, 12)
            parts = TimeParts(year=year, month=month + 1)
        else:
            parts = TimeParts(year=local.year + offset)
    return ResolvedTime(
        parts=parts,
        end=expression.end,
        range_kind=expression.range_kind,
        timezone=timezone,
        reference_at=stated_at,
    )


def render_time(value: ResolvedTime | None) -> str:
    if value is None:
        return "不明"

    def parts_text(parts: TimeParts) -> str:
        return "".join(
            str(number) + suffix
            for number, suffix in zip(
                (parts.year, parts.month, parts.day, parts.hour, parts.minute, parts.second),
                ("年", "月", "日", "時", "分", "秒"),
                strict=True,
            )
            if number is not None
        ) or "不明"

    result = parts_text(value.parts)
    if value.end is not None:
        label = "発生日時の可能範囲" if value.range_kind == "UNCERTAINTY" else "継続期間"
        result = label + ":" + result + "〜" + parts_text(value.end)
    return result + f"（{value.parts.precision}, {value.timezone}）"

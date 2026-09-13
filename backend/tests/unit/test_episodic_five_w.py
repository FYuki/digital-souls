"""欠損・出典・日時の境界を検証する。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.memory.episodic.contracts import (
    FiveW, NarrativeContext, Person, PersonRole, Place, ResolvedTime,
    SourceSpan, TimeExpression, TimeParts, What, text_slots,
)
from app.memory.episodic.temporal import render_time, resolve_time


def test_predicate_only_does_not_infer_owner_or_time() -> None:
    value = FiveW(what=What(predicate="話した"))
    assert value.who == ()
    assert value.when is None and value.where is None and value.why is None
    assert value.what.object is None and value.context is NarrativeContext.UNKNOWN


def test_relative_month_is_not_a_specific_day() -> None:
    result = resolve_time(
        TimeExpression(relative_unit="MONTH", relative_offset=-1),
        stated_at=datetime(2026, 10, 12, tzinfo=UTC), timezone="Asia/Tokyo",
    )
    assert result.parts == TimeParts(year=2026, month=9)
    assert result.parts.precision == "MONTH"
    assert result.parts.day is None
    assert "9月" in render_time(result) and "1日" not in render_time(result)


def test_relative_day_uses_source_timezone_not_worker_clock() -> None:
    # UTCでは11日だが、発言元の東京では12日。
    source = datetime(2026, 9, 11, 16, tzinfo=UTC)
    result = resolve_time(
        TimeExpression(relative_unit="DAY", relative_offset=-1),
        stated_at=source, timezone="Asia/Tokyo",
    )
    assert result.parts == TimeParts(year=2026, month=9, day=11)
    assert result.reference_at == source
    # 保存後の読込では現在のconfigを参照しない。
    restored = ResolvedTime.model_validate_json(result.model_dump_json())
    assert restored == result and restored.timezone == "Asia/Tokyo"


@pytest.mark.parametrize("parts,precision", [
    (TimeParts(year=2026), "YEAR"),
    (TimeParts(month=9), "PARTIAL"),
    (TimeParts(month=9, day=12), "PARTIAL"),
    (TimeParts(hour=12), "PARTIAL"),
    (TimeParts(), "UNKNOWN"),
])
def test_partial_dates_keep_known_components(parts: TimeParts, precision: str) -> None:
    result = resolve_time(
        TimeExpression(parts=parts),
        stated_at=datetime(2026, 9, 12, tzinfo=UTC), timezone="Asia/Tokyo",
    )
    assert result.parts == parts
    assert result.parts.precision == precision


def test_uncertainty_range_is_not_a_duration() -> None:
    source = datetime(2026, 9, 12, tzinfo=UTC)
    values = [
        resolve_time(
            TimeExpression(parts=TimeParts(year=2026, month=8),
                           end=TimeParts(year=2026, month=9), range_kind=kind),
            stated_at=source, timezone="Asia/Tokyo",
        ) for kind in ("UNCERTAINTY", "DURATION")
    ]
    assert "可能範囲" in render_time(values[0])
    assert "継続期間" in render_time(values[1])


@pytest.mark.parametrize("value", [
    {"year": 2025, "month": 2, "day": 29},
    {"month": 4, "day": 31},
    {"year": True},
    {"year": "2026"},
])
def test_invalid_date_parts_fail(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TimeParts.model_validate(value)


def test_model_cannot_supply_timezone_for_relative_expression() -> None:
    with pytest.raises(ValidationError):
        TimeExpression.model_validate({
            "relative_unit": "DAY", "relative_offset": -1, "timezone": "UTC",
        })


def test_source_requires_version_aware_time_and_nonempty_range() -> None:
    values = dict(source_id=uuid4(), revision=1, role="user", start=0, end=5,
                  stated_at=datetime(2026, 9, 12, tzinfo=UTC))
    assert SourceSpan.model_validate(values).revision == 1
    for change in (
        {"revision": 0}, {"end": 0}, {"end": 3, "start": 3},
        {"stated_at": datetime(2026, 9, 12)},
    ):
        with pytest.raises(ValidationError):
            SourceSpan.model_validate(values | change)


def test_privacy_slots_include_names_roles_targets_place_and_reason() -> None:
    value = FiveW(
        who=(Person(name="利用者", role=PersonRole.ACTOR, entity_id="speaker:user"),),
        what=What(predicate="訪れた", object="店"),
        where=Place(name="駅前", entity_id="place:station"),
        why="昼食のため",
    )
    assert set(text_slots(value)) == {
        "利用者", "speaker:user", "訪れた", "店", "駅前", "place:station", "昼食のため",
    }

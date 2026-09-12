from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.memory.episodic.contracts import ResolvedTime, TimeParts
from app.memory.episodic.time_search import matches_time

ZONE = ZoneInfo("Asia/Tokyo")
START = datetime(2026, 8, 1, tzinfo=ZONE)
END = datetime(2026, 9, 1, tzinfo=ZONE)


@pytest.mark.parametrize("parts, matched", [
    (TimeParts(year=2026, month=8), True),
    (TimeParts(year=2026, month=8, day=10), True),
    (TimeParts(year=2026), False),
    (TimeParts(month=8), False),
    (TimeParts(year=2026, day=5), False),
    (TimeParts(), False),
    (TimeParts(year=2026, month=9), False),
    (TimeParts(year=9999), False),
])
def test_precision_and_unknowns_do_not_fabricate_period_matches(parts, matched):
    value = ResolvedTime(parts=parts, timezone="Asia/Tokyo", reference_at=START)
    assert matches_time(value, START, END) is matched


def test_duration_overlap_and_uncertain_range_have_different_meanings():
    values = dict(parts=TimeParts(year=2026, month=7, day=30),
                  end=TimeParts(year=2026, month=9, day=3),
                  timezone="Asia/Tokyo", reference_at=START)
    assert matches_time(ResolvedTime(**values, range_kind="DURATION"), START, END)
    assert not matches_time(ResolvedTime(**values, range_kind="UNCERTAINTY"), START, END)

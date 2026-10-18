from datetime import UTC, datetime, timedelta, timezone

import pytest

from digital_souls_core.history import as_utc, current_utc

from .time_support import FIRST


@pytest.mark.ut
def test_utc_normalization_preserves_instant_and_rejects_naive_time() -> None:
    assert as_utc(FIRST.astimezone(timezone(timedelta(hours=9)))) == FIRST
    assert as_utc(FIRST).tzinfo == UTC
    with pytest.raises(ValueError):
        as_utc(FIRST.replace(tzinfo=None))


@pytest.mark.ut
def test_current_utc_returns_current_aware_time() -> None:
    before = datetime.now(UTC)
    value = current_utc()
    assert before <= value <= datetime.now(UTC)
    assert value.tzinfo == UTC

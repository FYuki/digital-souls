from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping


def current_timestamp() -> str:
    return _wall_clock_timestamp().isoformat()


def next_lifecycle_timestamp(report: Mapping[str, object]) -> str:
    current = _wall_clock_timestamp()
    prior = max(
        _timestamp(report.get(name))
        for name in ("startedAt", "readyAt", "endedAt")
        if report.get(name) is not None
    )
    return max(current, prior).isoformat()


def _wall_clock_timestamp() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("lifecycle timestamp must be a string")
    normalized = value.replace("Z", "+00:00").replace("z", "+00:00")
    return datetime.fromisoformat(normalized)

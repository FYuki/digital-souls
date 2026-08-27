from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    name: str
    value: int
    clock_domain: str
    unit: str


@dataclass(frozen=True)
class Duration:
    value: int
    clock_domain: str
    unit: str


def elapsed(end: Observation, start: Observation) -> Duration:
    if end.clock_domain != start.clock_domain or end.unit != start.unit:
        raise ValueError("observations must share a clock domain and unit")
    return Duration(end.value - start.value, end.clock_domain, end.unit)


class ReconnectReadiness:
    def __init__(self) -> None:
        self._control_at: int | None = None
        self._audio_at: int | None = None

    @property
    def completed_at(self) -> int | None:
        if self._control_at is None or self._audio_at is None:
            return None
        return max(self._control_at, self._audio_at)

    def control_available(self, *, at: int) -> None:
        self._control_at = at

    def audio_available(self, *, at: int) -> None:
        self._audio_at = at


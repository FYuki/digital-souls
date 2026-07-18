from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvironmentTiming:
    readiness_attempts: int = 120
    readiness_interval_seconds: float = 0.5
    request_timeout_seconds: float = 1.0
    supervision_interval_seconds: float = 0.5

    def __post_init__(self) -> None:
        if (
            isinstance(self.readiness_attempts, bool)
            or not isinstance(self.readiness_attempts, int)
            or self.readiness_attempts <= 0
        ):
            raise ValueError("readiness_attempts must be a positive integer")
        for name in (
            "readiness_interval_seconds",
            "request_timeout_seconds",
            "supervision_interval_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative number")

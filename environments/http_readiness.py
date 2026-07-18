from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable


class ReadinessConfigurationError(ValueError):
    __slots__ = ()


@dataclass(frozen=True)
class ReadinessResult:
    url: str
    attempts: int
    elapsed_seconds: float
    result: str

    def to_report(self) -> dict[str, object]:
        return {
            "url": self.url,
            "attempts": self.attempts,
            "elapsedSeconds": self.elapsed_seconds,
            "result": self.result,
        }


def _require_non_negative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ReadinessConfigurationError(f"{name} must be a non-negative number")
    return float(value)


def probe_http(url: str, *, timeout_seconds: float) -> ReadinessResult:
    timeout = _require_non_negative_number(timeout_seconds, "timeout_seconds")
    started = time.monotonic()
    result = "not_ready"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if 200 <= response.status < 400:
                result = "ready"
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return ReadinessResult(url, 1, time.monotonic() - started, result)
    return ReadinessResult(url, 1, time.monotonic() - started, result)


def wait_for_http(
    url: str,
    *,
    max_attempts: int,
    interval_seconds: float,
    request_timeout_seconds: float,
    assert_environment_running: Callable[[], None] | None = None,
) -> ReadinessResult:
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0:
        raise ReadinessConfigurationError("max_attempts must be a positive integer")
    interval = _require_non_negative_number(interval_seconds, "interval_seconds")
    _require_non_negative_number(request_timeout_seconds, "request_timeout_seconds")
    started = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        if assert_environment_running is not None:
            assert_environment_running()
        observation = probe_http(url, timeout_seconds=request_timeout_seconds)
        if observation.result == "ready":
            if assert_environment_running is not None:
                assert_environment_running()
            return ReadinessResult(url, attempt, time.monotonic() - started, "ready")
        if attempt < max_attempts:
            time.sleep(interval)
    return ReadinessResult(url, max_attempts, time.monotonic() - started, "timeout")

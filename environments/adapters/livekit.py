from __future__ import annotations

from typing import Mapping

from adapters.base import ReadinessValidationResult
from http_readiness import ReadinessResult, probe_http


class LiveKitExternalOperations:
    def probe(self, dependency: Mapping[str, object], timeout_seconds: float) -> ReadinessResult:
        readiness_url = dependency.get("readinessUrl")
        if not isinstance(readiness_url, str):
            raise ValueError("livekit readinessUrl is required")
        return probe_http(readiness_url, timeout_seconds=timeout_seconds)

    def validate_readiness(
        self, dependency: Mapping[str, object]
    ) -> ReadinessValidationResult:
        del dependency
        return ReadinessValidationResult("ready")

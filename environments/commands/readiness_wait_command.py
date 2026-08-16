from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast

from environment_timing import EnvironmentTiming
from http_readiness import wait_for_http
from profile_resolution import resolve_dependencies
from profile_types import ProfileError
from profile_validation import load_profile

INFERENCE_SERVICE_NAMES = ("ollama", "voicevox")


def wait_for_inference_services(
    profile_name: str,
    service_names: Sequence[str],
    timing: EnvironmentTiming,
) -> int:
    profile = load_profile(profile_name)
    dependencies = resolve_dependencies(profile["dependencies"])
    dependency_map = cast(Mapping[str, Mapping[str, object]], dependencies)
    validated_services: list[tuple[str, str]] = []

    for name in service_names:
        if name not in INFERENCE_SERVICE_NAMES:
            raise ProfileError(f"unsupported inference readiness service: {name}")
        if name not in dependency_map:
            raise ProfileError(f"{name} dependency is required")
        dependency = dependency_map[name]
        if dependency.get("mode") != "real" or dependency.get("source") != "external":
            raise ProfileError(
                f"{name} must be configured as a real external dependency"
            )
        readiness_url = dependency.get("readinessUrl")
        if not isinstance(readiness_url, str) or not readiness_url:
            raise ProfileError(f"{name} readinessUrl is required")
        validated_services.append((name, readiness_url))

    observations: dict[str, dict[str, object]] = {}
    ready = True
    for name, readiness_url in validated_services:
        observation = wait_for_http(
            readiness_url,
            max_attempts=timing.readiness_attempts,
            interval_seconds=timing.readiness_interval_seconds,
            request_timeout_seconds=timing.request_timeout_seconds,
        )
        observations[name] = observation.to_report()
        ready = ready and observation.result == "ready"

    report = {
        "status": "ready" if ready else "not_ready",
        "profile": profile["name"],
        "services": observations,
    }
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if ready else 1

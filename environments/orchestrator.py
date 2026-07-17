from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from environment_constants import HTTP_SERVICE_NAMES


FAILURE_CATEGORY_BY_PHASE = {
    "resolve": "profile",
    "verify": "preparation",
    "prepare": "preparation",
    "pre_probe": "readiness",
    "start_or_reuse": "startup",
    "start": "startup",
    "readiness": "readiness",
    "ready": "supervision",
    "supervise": "supervision",
    "cleanup": "teardown",
}


@dataclass(frozen=True)
class PreprobeResult:
    state: str
    failure_category: str | None


def classify_failure(phase: str) -> dict[str, str]:
    if phase not in FAILURE_CATEGORY_BY_PHASE:
        raise ValueError(f"unknown lifecycle phase: {phase}")
    return {"category": FAILURE_CATEGORY_BY_PHASE[phase]}


def classify_preprobe(
    dependency: Mapping[str, object], observation: Mapping[str, object]
) -> PreprobeResult:
    source = dependency.get("source")
    ready = observation.get("result") == "ready"
    if source == "external":
        return PreprobeResult("external", None if ready else "readiness")
    if source != "managed":
        raise ValueError("pre-probe only accepts managed or external HTTP services")
    if ready:
        return PreprobeResult("reused", None)
    return PreprobeResult("start_required", None)


def classify_verification(
    *, source: str, prerequisites_ready: bool, probe_result: str
) -> str:
    if probe_result == "pending":
        return "pending"
    if source == "external":
        return "ready" if probe_result == "ready" else "readiness"
    if source != "managed":
        raise ValueError(f"unsupported verification source: {source}")
    if probe_result == "ready":
        return "ready"
    return "start_required" if prerequisites_ready else "preparation_required"


def readiness_complete(
    profile: Mapping[str, object], observations: Mapping[str, Mapping[str, object]]
) -> bool:
    dependencies = profile.get("dependencies")
    if not isinstance(dependencies, dict):
        raise ValueError("resolved profile dependencies are required")
    active_http = (
        name
        for name in HTTP_SERVICE_NAMES
        if isinstance(dependencies.get(name), dict)
        and dependencies[name].get("mode") != "disabled"
        and dependencies[name].get("source") != "browser"
    )
    return all(
        name in observations and observations[name].get("result") == "ready"
        for name in active_http
    )


def cleanup_owned_services(
    report: Mapping[str, object], stoppers: Mapping[str, Callable[[], Mapping[str, object]]]
) -> list[dict[str, object]]:
    services = report.get("services")
    sequence = report.get("startSequence")
    if not isinstance(services, dict) or not isinstance(sequence, list):
        raise ValueError("invalid run report cleanup data")
    results: list[dict[str, object]] = []
    for name in reversed(sequence):
        service = services.get(name)
        if (
            not isinstance(service, dict)
            or service.get("source") != "managed"
            or service.get("state") != "started"
            or service.get("owned") is not True
        ):
            continue
        try:
            result = dict(stoppers[name]())
        except Exception as error:
            result = {"result": "failed", "message": str(error)}
        results.append({"service": name, **result})
    return results

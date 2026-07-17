from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit

from environment_constants import (
    CLEANUP_TARGET_NAMES,
    DEPENDENCY_NAMES,
    RUN_REPORT_SCHEMA_VERSION,
)
from run_report_contract import (
    FAILURE_CATEGORIES,
    MODES,
    PHASES,
    REPORT_FIELDS,
    SOURCES,
    STATES,
    STATUSES,
    RunReportError,
)


SCHEMA_PATH = Path(__file__).parent / "schemas" / "environment-run-v1.schema.json"


def _date_time_pattern() -> re.Pattern[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    pattern = schema["$defs"]["dateTime"]["pattern"]
    if not isinstance(pattern, str):
        raise RuntimeError("run report Schema dateTime pattern is invalid")
    return re.compile(pattern)


DATE_TIME_PATTERN = _date_time_pattern()


def _is_datetime(value: object, *, nullable: bool) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str) or DATE_TIME_PATTERN.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _is_uri(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return bool(parsed.scheme and parsed.netloc)


def _is_process_identity(value: object) -> bool:
    expected = {"pid", "pgid", "sessionId", "startTime"}
    return (
        isinstance(value, dict)
        and set(value) == expected
        and all(
            not isinstance(value[field], bool)
            and isinstance(value[field], int)
            and value[field] > 0
            for field in expected
        )
    )


def _validate_identity(name: str, value: Mapping[str, object]) -> None:
    process_identity = value["processIdentity"]
    if process_identity is not None and not _is_process_identity(process_identity):
        raise RunReportError(f"services.{name} has invalid process identity")
    container_identity = value["containerIdentity"]
    if container_identity is not None and (
        not isinstance(container_identity, dict)
        or set(container_identity) != {"containerId", "startedAt"}
        or any(not isinstance(field, str) or not field for field in container_identity.values())
    ):
        raise RunReportError(f"services.{name} has invalid container identity")


def _validate_readiness(name: str, readiness: object) -> None:
    if readiness is None:
        return
    if (
        not isinstance(readiness, dict)
        or set(readiness) != {"url", "attempts", "elapsedSeconds", "result"}
        or not _is_uri(readiness["url"])
        or isinstance(readiness["attempts"], bool)
        or not isinstance(readiness["attempts"], int)
        or readiness["attempts"] <= 0
        or isinstance(readiness["elapsedSeconds"], bool)
        or not isinstance(readiness["elapsedSeconds"], (int, float))
        or readiness["elapsedSeconds"] < 0
        or not isinstance(readiness["result"], str)
        or readiness["result"] not in {"ready", "not_ready", "timeout"}
    ):
        raise RunReportError(f"services.{name} has invalid readiness observation")


def _validate_service(name: str, value: object) -> None:
    fields = {
        "mode", "source", "state", "owned", "processIdentity",
        "containerIdentity", "readiness",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RunReportError(f"services.{name} has invalid fields")
    if (
        not (value["mode"] is None or isinstance(value["mode"], str))
        or value["mode"] not in MODES
        or not (value["source"] is None or isinstance(value["source"], str))
        or value["source"] not in SOURCES
    ):
        raise RunReportError(f"services.{name} has invalid mode or source")
    if (
        not isinstance(value["state"], str)
        or value["state"] not in STATES
        or not isinstance(value["owned"], bool)
    ):
        raise RunReportError(f"services.{name} has invalid state or ownership")
    if (value["state"] == "started") != value["owned"]:
        raise RunReportError(f"services.{name} started state and ownership must agree")
    if value["state"] == "started" and value["source"] != "managed":
        raise RunReportError(f"services.{name} started state requires managed source")
    allowed_states = {
        "managed": {"pending", "started", "reused"},
        "external": {"external"},
        "in_process": {"in_process"},
        "browser": {"browser"},
        None: {"pending", "disabled"},
    }
    if value["state"] not in allowed_states[value["source"]]:
        raise RunReportError(
            f"services.{name} source and state must agree"
        )
    if value["source"] is None and not (
        (value["mode"] == "disabled" and value["state"] == "disabled")
        or (value["mode"] is None and value["state"] == "pending")
    ):
        raise RunReportError(f"services.{name} mode and state must agree")
    identities = (value["processIdentity"], value["containerIdentity"])
    if value["owned"] and all(identity is None for identity in identities):
        raise RunReportError(f"services.{name} ownership requires an identity")
    if any(identity is not None for identity in identities) and not value["owned"]:
        raise RunReportError(f"services.{name} identity requires ownership")
    if all(identity is not None for identity in identities):
        raise RunReportError(f"services.{name} has multiple identities")
    if name == "voicevox" and value["processIdentity"] is not None:
        raise RunReportError("services.voicevox requires container identity")
    if name in {"frontend", "backend", "ollama"} and value["containerIdentity"] is not None:
        raise RunReportError(f"services.{name} requires process identity")
    if name in {"whisper", "chroma"} and any(identity is not None for identity in identities):
        raise RunReportError(f"services.{name} cannot have a runtime identity")
    _validate_identity(name, value)
    _validate_readiness(name, value["readiness"])


def _validate_top_level(report: Mapping[str, object]) -> None:
    if "orchestratorIdentity" not in report:
        raise RunReportError("missing orchestratorIdentity")
    if set(report) != REPORT_FIELDS:
        raise RunReportError("run report has unknown or missing fields")
    if report["schemaVersion"] != RUN_REPORT_SCHEMA_VERSION or isinstance(
        report["schemaVersion"], bool
    ):
        raise RunReportError("invalid schemaVersion")
    if not isinstance(report["runId"], str) or not report["runId"]:
        raise RunReportError("invalid runId")
    if not _is_datetime(report["startedAt"], nullable=False):
        raise RunReportError("invalid startedAt")
    if not _is_datetime(report["readyAt"], nullable=True) or not _is_datetime(
        report["endedAt"], nullable=True
    ):
        raise RunReportError("invalid lifecycle timestamp")
    if not isinstance(report["resolvedProfilePath"], str) or not report["resolvedProfilePath"]:
        raise RunReportError("invalid resolvedProfilePath")
    orchestrator_identity = report["orchestratorIdentity"]
    if not _is_process_identity(orchestrator_identity):
        raise RunReportError("invalid orchestratorIdentity")
    if report["effectiveProfile"] is not None and not isinstance(report["effectiveProfile"], dict):
        raise RunReportError("invalid effectiveProfile")
    if (
        not isinstance(report["phase"], str)
        or report["phase"] not in PHASES
        or not isinstance(report["status"], str)
        or report["status"] not in STATUSES
    ):
        raise RunReportError("invalid phase or status")


def _validate_services(report: Mapping[str, object]) -> None:
    services = report["services"]
    if not isinstance(services, dict) or set(services) != set(DEPENDENCY_NAMES):
        raise RunReportError("services must contain all dependencies")
    for name, value in services.items():
        _validate_service(name, value)
    sequence = report["startSequence"]
    if not isinstance(sequence, list) or any(
        not isinstance(service, str) or service not in DEPENDENCY_NAMES
        for service in sequence
    ):
        raise RunReportError("invalid startSequence")
    if len(sequence) != len(set(sequence)):
        raise RunReportError("startSequence must contain unique services")
    owned_services = {
        name for name, service in services.items()
        if isinstance(service, dict) and service["owned"] is True
    }
    if set(sequence) != owned_services:
        raise RunReportError("startSequence must contain exactly the owned services")


def _validate_effective_profile_correlation(report: Mapping[str, object]) -> None:
    effective_profile = report["effectiveProfile"]
    if effective_profile is None:
        return
    if not isinstance(effective_profile, dict):
        raise RunReportError("invalid effectiveProfile")
    dependencies = effective_profile.get("dependencies")
    if not isinstance(dependencies, dict) or set(dependencies) != set(DEPENDENCY_NAMES):
        raise RunReportError("effectiveProfile must contain all dependencies")
    services = report["services"]
    if not isinstance(services, dict):
        raise RunReportError("services must contain all dependencies")
    for name in DEPENDENCY_NAMES:
        dependency = dependencies[name]
        service = services[name]
        if not isinstance(dependency, dict) or not isinstance(service, dict):
            raise RunReportError(f"effectiveProfile dependency {name} is invalid")
        if service["mode"] != dependency.get("mode"):
            raise RunReportError(f"services.{name} mode must match effectiveProfile")
        if service["source"] != dependency.get("source"):
            raise RunReportError(f"services.{name} source must match effectiveProfile")


def _validate_outcomes(report: Mapping[str, object]) -> None:
    failure = report["failure"]
    if failure is not None and (
        not isinstance(failure, dict)
        or set(failure) != {"category", "message"}
        or not isinstance(failure["category"], str)
        or failure["category"] not in FAILURE_CATEGORIES
        or not isinstance(failure["message"], str)
    ):
        raise RunReportError("invalid failure")
    test_result = report["testResult"]
    if test_result is not None and (
        not isinstance(test_result, dict)
        or set(test_result) != {"status", "endedAt", "message"}
        or not isinstance(test_result["status"], str)
        or test_result["status"] not in {"passed", "failed"}
        or not _is_datetime(test_result["endedAt"], nullable=False)
        or not isinstance(test_result["message"], str)
    ):
        raise RunReportError("invalid testResult")


def _validate_teardown(report: Mapping[str, object]) -> None:
    teardown = report["teardown"]
    if not isinstance(teardown, dict) or set(teardown) != {"status", "results"}:
        raise RunReportError("invalid teardown")
    if (
        not isinstance(teardown["status"], str)
        or teardown["status"] not in {"pending", "completed", "failed"}
        or not isinstance(teardown["results"], list)
    ):
        raise RunReportError("invalid teardown state")
    for result in teardown["results"]:
        if (
            not isinstance(result, dict)
            or not {"service", "result"}.issubset(result)
            or set(result) - {"service", "result", "message"}
            or not isinstance(result["service"], str)
            or result["service"] not in CLEANUP_TARGET_NAMES
            or not isinstance(result["result"], str)
            or ("message" in result and not isinstance(result["message"], str))
        ):
            raise RunReportError("invalid teardown result")


def validate_run_report_shape(report: Mapping[str, object]) -> None:
    _validate_top_level(report)
    _validate_services(report)
    _validate_effective_profile_correlation(report)
    _validate_outcomes(report)
    _validate_teardown(report)

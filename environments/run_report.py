from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Mapping, cast

from environment_constants import DEPENDENCY_NAMES, RUN_REPORT_SCHEMA_VERSION
from run_report_validation import (
    FAILURE_CATEGORIES,
    PHASES,
    STATES,
    RunReportError,
    validate_run_report,
)


def _state_for_dependency(dependency: Mapping[str, object] | None) -> str:
    if dependency is None:
        return "pending"
    if dependency.get("mode") == "disabled":
        return "disabled"
    source = dependency.get("source")
    if source in {"in_process", "browser", "external"}:
        return str(source)
    return "pending"


def _service_record(dependency: Mapping[str, object] | None) -> dict[str, object]:
    return {
        "mode": None if dependency is None else dependency.get("mode"),
        "source": None if dependency is None else dependency.get("source"),
        "state": _state_for_dependency(dependency),
        "owned": False,
        "processIdentity": None,
        "containerIdentity": None,
        "readiness": None,
    }


def create_pending_report(
    *,
    run_id: str,
    started_at: str,
    resolved_profile_path: Path,
    orchestrator_identity: Mapping[str, int],
) -> dict[str, object]:
    report = {
        "schemaVersion": RUN_REPORT_SCHEMA_VERSION,
        "runId": run_id,
        "startedAt": started_at,
        "readyAt": None,
        "endedAt": None,
        "resolvedProfilePath": str(resolved_profile_path),
        "orchestratorIdentity": dict(orchestrator_identity),
        "effectiveProfile": None,
        "phase": "resolve",
        "status": "running",
        "startSequence": [],
        "services": {name: _service_record(None) for name in DEPENDENCY_NAMES},
        "testResult": None,
        "failure": None,
        "teardown": {"status": "pending", "results": []},
    }
    validate_run_report(report)
    return report


def create_initial_report(
    *,
    run_id: str,
    started_at: str,
    resolved_profile_path: Path,
    effective_profile: Mapping[str, object],
    orchestrator_identity: Mapping[str, int],
) -> dict[str, object]:
    dependencies = effective_profile.get("dependencies")
    if not isinstance(dependencies, dict) or set(dependencies) != set(DEPENDENCY_NAMES):
        raise RunReportError("effective profile must define all dependencies")
    pending = create_pending_report(
        run_id=run_id,
        started_at=started_at,
        resolved_profile_path=resolved_profile_path,
        orchestrator_identity=orchestrator_identity,
    )
    report = {
        **pending,
        "effectiveProfile": deepcopy(dict(effective_profile)),
        "phase": "verify",
        "services": {
            name: _service_record(dependencies[name]) for name in DEPENDENCY_NAMES
        },
    }
    validate_run_report(report)
    return report


def _require_service(report: Mapping[str, object], service: str) -> Mapping[str, object]:
    services = report.get("services")
    if not isinstance(services, dict) or service not in services:
        raise RunReportError(f"unknown service: {service}")
    value = services[service]
    if not isinstance(value, dict):
        raise RunReportError(f"invalid service: {service}")
    return value


def update_service(
    report: Mapping[str, object],
    service: str,
    *,
    state: str,
    owned: bool,
    process_identity: Mapping[str, int] | None = None,
    container_identity: Mapping[str, str] | None = None,
) -> dict[str, object]:
    if state not in STATES:
        raise RunReportError(f"unknown service state: {state}")
    if owned and state != "started":
        raise RunReportError(f"state {state} cannot be owned")
    if owned and process_identity is None and container_identity is None:
        raise RunReportError("owned started service requires an identity")
    if (process_identity is not None or container_identity is not None) and not (owned and state == "started"):
        raise RunReportError("identity requires an owned started service")
    if process_identity is not None and container_identity is not None:
        raise RunReportError("service cannot have process and container identities")
    current = _require_service(report, service)
    updated = deepcopy(dict(report))
    updated_service = {
        **current,
        "state": state,
        "owned": owned,
        "processIdentity": None if process_identity is None else dict(process_identity),
        "containerIdentity": None if container_identity is None else dict(container_identity),
    }
    services = cast(dict[str, dict[str, object]], updated["services"])
    updated["services"] = {**services, service: updated_service}
    sequence = list(cast(list[str], updated["startSequence"]))
    if owned and service not in sequence:
        sequence.append(service)
    updated["startSequence"] = sequence
    validate_run_report(updated)
    return updated


def update_readiness(
    report: Mapping[str, object], service: str, readiness: Mapping[str, object]
) -> dict[str, object]:
    current = _require_service(report, service)
    updated = deepcopy(dict(report))
    services = cast(dict[str, dict[str, object]], updated["services"])
    updated["services"] = {
        **services,
        service: {**current, "readiness": dict(readiness)},
    }
    validate_run_report(updated)
    return updated


def record_phase(report: Mapping[str, object], phase: str) -> dict[str, object]:
    if phase not in PHASES:
        raise RunReportError(f"unknown lifecycle phase: {phase}")
    updated = {**deepcopy(dict(report)), "phase": phase}
    validate_run_report(updated)
    return updated


def restore_ownership_history(
    report: Mapping[str, object], ownership_report: Mapping[str, object]
) -> dict[str, object]:
    updated = deepcopy(dict(report))
    updated_services = cast(dict[str, dict[str, object]], updated["services"])
    ownership_services = cast(
        Mapping[str, Mapping[str, object]], ownership_report["services"]
    )
    sequence = cast(list[str], ownership_report["startSequence"])
    for name in sequence:
        ownership = ownership_services[name]
        updated_services = {
            **updated_services,
            name: {
                **updated_services[name],
                "state": ownership["state"],
                "owned": ownership["owned"],
                "processIdentity": deepcopy(ownership["processIdentity"]),
                "containerIdentity": deepcopy(ownership["containerIdentity"]),
            },
        }
    updated["services"] = updated_services
    updated["startSequence"] = list(sequence)
    validate_run_report(updated)
    return updated


def record_cleanup_started(report: Mapping[str, object]) -> dict[str, object]:
    if report.get("phase") == "complete":
        updated = deepcopy(dict(report))
        validate_run_report(updated)
        return updated
    return record_phase(report, "cleanup")


def record_ready(report: Mapping[str, object], *, ready_at: str) -> dict[str, object]:
    updated = deepcopy(dict(report))
    updated.update(readyAt=ready_at, phase="ready", status="ready", failure=None)
    validate_run_report(updated)
    return updated


def record_failure(
    report: Mapping[str, object], *, category: str, message: str
) -> dict[str, object]:
    if category not in FAILURE_CATEGORIES:
        raise RunReportError(f"unknown failure category: {category}")
    updated = deepcopy(dict(report))
    updated.update(status="failed", failure={"category": category, "message": message})
    validate_run_report(updated)
    return updated


def record_test_result(
    report: Mapping[str, object], *, status: str, ended_at: str, message: str
) -> dict[str, object]:
    if status not in {"passed", "failed"}:
        raise RunReportError(f"unknown test result: {status}")
    updated = deepcopy(dict(report))
    updated["testResult"] = {"status": status, "endedAt": ended_at, "message": message}
    if status == "failed" and updated["failure"] is None:
        updated["status"] = "failed"
        updated["failure"] = {"category": "test", "message": message}
    validate_run_report(updated)
    return updated


def record_cleanup(
    report: Mapping[str, object], *, results: list[dict[str, object]], ended_at: str
) -> dict[str, object]:
    updated = deepcopy(dict(report))
    teardown = cast(Mapping[str, object], updated["teardown"])
    prior_results = cast(list[dict[str, object]], teardown["results"])
    cumulative_results = [*deepcopy(prior_results), *deepcopy(results)]
    failure = updated["failure"]
    failed = (
        teardown["status"] == "failed"
        or (
            isinstance(failure, dict)
            and failure.get("category") == "teardown"
        )
        or any(result.get("result") == "failed" for result in cumulative_results)
    )
    updated["phase"] = "complete"
    updated["endedAt"] = ended_at
    updated["teardown"] = {
        "status": "failed" if failed else "completed",
        "results": cumulative_results,
    }
    if failed and updated["failure"] is None:
        updated["failure"] = {"category": "teardown", "message": "environment cleanup failed"}
    updated["status"] = "failed" if updated["failure"] is not None else "completed"
    validate_run_report(updated)
    return updated

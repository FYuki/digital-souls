from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from tests.environment_test_support import (
    DEPENDENCY_NAMES,
    orchestrator_identity,
    resolved_profile,
)


ROOT_DIR = Path(__file__).parent.parent.parent.parent
ENVIRONMENTS_DIR = ROOT_DIR / "environments"


def _initial_report():
    from run_report import create_initial_report

    return create_initial_report(
        run_id="run-001",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=Path("/runtime/resolved-profile.json"),
        effective_profile=resolved_profile(),
        orchestrator_identity=orchestrator_identity(),
    )


@pytest.fixture
def report_validator() -> Draft202012Validator:
    schema = json.loads(
        (ENVIRONMENTS_DIR / "schemas" / "environment-run-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_should_create_schema_valid_report_before_profile_resolution(
    report_validator: Draft202012Validator,
):
    from run_report import create_pending_report

    report = create_pending_report(
        run_id="run-profile-failure",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=Path("/runtime/resolved-profile.json"),
        orchestrator_identity=orchestrator_identity(),
    )

    report_validator.validate(report)
    assert report["phase"] == "resolve"
    assert report["status"] == "running"
    assert report["effectiveProfile"] is None
    assert set(report["services"]) == set(DEPENDENCY_NAMES)
    assert report["orchestratorIdentity"] == orchestrator_identity()


def test_should_require_orchestrator_identity_in_runtime_report(
    report_validator: Draft202012Validator,
):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    report.pop("orchestratorIdentity")

    with pytest.raises(RunReportError, match="orchestratorIdentity"):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report))


def test_should_require_orchestrator_identity_when_creating_pending_report():
    from run_report import create_pending_report

    with pytest.raises(TypeError, match="orchestrator_identity"):
        create_pending_report(
            run_id="missing-orchestrator-identity",
            started_at="2026-07-17T00:00:00+00:00",
            resolved_profile_path=Path("/runtime/resolved-profile.json"),
        )


@pytest.mark.parametrize(
    "identity",
    [
        None,
        {"pid": 3101, "pgid": 3101, "sessionId": 3101},
        {"pid": True, "pgid": 3101, "sessionId": 3101, "startTime": 99101},
        {
            "pid": 3101,
            "pgid": 3101,
            "sessionId": 3101,
            "startTime": 99101,
            "command": "up.sh",
        },
    ],
)
def test_should_reject_invalid_orchestrator_identity_consistently_with_schema(
    report_validator: Draft202012Validator, identity: object
):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    report["orchestratorIdentity"] = identity

    with pytest.raises(RunReportError, match="orchestratorIdentity"):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report))


def test_should_map_all_profile_dependencies_to_initial_runtime_states(
    report_validator: Draft202012Validator,
):
    report = _initial_report()

    report_validator.validate(report)
    assert report["services"]["frontend"]["state"] == "pending"
    assert report["services"]["whisper"]["state"] == "in_process"
    assert report["services"]["chroma"]["state"] == "disabled"
    assert all(service["owned"] is False for service in report["services"].values())


@pytest.mark.parametrize(
    "state",
    ["reused", "external", "in_process", "browser", "disabled"],
)
def test_should_reject_owned_state_that_cannot_be_stopped(state: str):
    from run_report import RunReportError, update_service

    report = _initial_report()

    with pytest.raises(RunReportError, match="owned"):
        update_service(report, "frontend", state=state, owned=True)


def test_should_record_process_identity_only_for_started_owned_service():
    from run_report import update_service

    report = _initial_report()
    identity = {"pid": 4101, "pgid": 4101, "sessionId": 4101, "startTime": 88201}

    updated = update_service(
        report,
        "backend",
        state="started",
        owned=True,
        process_identity=identity,
    )

    assert updated["services"]["backend"]["processIdentity"] == identity
    assert updated["startSequence"] == ["backend"]
    assert report["services"]["backend"]["state"] == "pending"


def test_should_record_readiness_observations_without_rounding_attempts():
    from run_report import update_readiness

    report = _initial_report()
    readiness = {
        "url": "http://localhost:5173/",
        "attempts": 3,
        "elapsedSeconds": 0.0125,
        "result": "ready",
    }

    updated = update_readiness(report, "frontend", readiness)

    assert updated["services"]["frontend"]["readiness"] == readiness


def test_should_record_ready_timestamp_separately_from_later_test_process_outcome():
    from run_report import record_ready

    updated = record_ready(_initial_report(), ready_at="2026-07-17T00:00:30+00:00")

    assert updated["readyAt"] == "2026-07-17T00:00:30+00:00"
    assert updated["phase"] == "ready"
    assert updated["status"] == "ready"
    assert updated["failure"] is None


def test_should_preserve_primary_failure_when_cleanup_also_fails():
    from run_report import record_cleanup, record_failure

    report = record_failure(
        _initial_report(),
        category="readiness",
        message="frontend did not become ready",
    )
    updated = record_cleanup(
        report,
        results=[
            {
                "service": "backend",
                "result": "failed",
                "message": "TERM timed out",
            }
        ],
        ended_at="2026-07-17T00:01:00+00:00",
    )

    assert updated["failure"]["category"] == "readiness"
    assert updated["teardown"]["status"] == "failed"
    assert updated["teardown"]["results"][0]["service"] == "backend"


def test_should_use_teardown_as_primary_category_only_without_prior_failure():
    from run_report import record_cleanup

    updated = record_cleanup(
        _initial_report(),
        results=[{"service": "frontend", "result": "failed", "message": "KILL failed"}],
        ended_at="2026-07-17T00:01:00+00:00",
    )

    assert updated["failure"]["category"] == "teardown"


@pytest.mark.parametrize("later_result", ["stopped_term", "skipped_identity_mismatch"])
def test_should_append_cleanup_retry_after_prior_failure_without_clearing_evidence(
    later_result: str,
):
    from run_report import record_cleanup

    first_attempt = record_cleanup(
        _initial_report(),
        results=[
            {"service": "backend", "result": "failed", "message": "TERM timed out"}
        ],
        ended_at="2026-07-17T00:01:00+00:00",
    )

    retried = record_cleanup(
        first_attempt,
        results=[{"service": "backend", "result": later_result}],
        ended_at="2026-07-17T00:02:00+00:00",
    )

    assert retried["teardown"] == {
        "status": "failed",
        "results": [
            {
                "service": "backend",
                "result": "failed",
                "message": "TERM timed out",
            },
            {"service": "backend", "result": later_result},
        ],
    }
    assert retried["failure"] == first_attempt["failure"]
    assert retried["status"] == "failed"
    assert retried["endedAt"] == "2026-07-17T00:02:00+00:00"


def test_should_preserve_failed_teardown_without_result_evidence_on_retry():
    from run_report import record_cleanup, validate_run_report

    prior_failure = record_cleanup(
        _initial_report(),
        results=[
            {"service": "backend", "result": "failed", "message": "stop failed"}
        ],
        ended_at="2026-07-17T00:01:00+00:00",
    )
    prior_failure["teardown"]["results"] = []
    validate_run_report(prior_failure)

    retried = record_cleanup(
        prior_failure,
        results=[{"service": "backend", "result": "skipped_identity_mismatch"}],
        ended_at="2026-07-17T00:02:00+00:00",
    )

    assert retried["teardown"] == {
        "status": "failed",
        "results": [
            {"service": "backend", "result": "skipped_identity_mismatch"},
        ],
    }
    assert retried["failure"] == prior_failure["failure"]
    assert retried["status"] == "failed"


def test_should_complete_cleanup_when_accumulated_history_has_no_failure():
    from run_report import record_cleanup, record_ready

    first_attempt = record_cleanup(
        record_ready(_initial_report(), ready_at="2026-07-17T00:00:30+00:00"),
        results=[{"service": "frontend", "result": "stopped_term"}],
        ended_at="2026-07-17T00:01:00+00:00",
    )

    retried = record_cleanup(
        first_attempt,
        results=[{"service": "backend", "result": "skipped_identity_mismatch"}],
        ended_at="2026-07-17T00:02:00+00:00",
    )

    assert retried["teardown"] == {
        "status": "completed",
        "results": [
            {"service": "frontend", "result": "stopped_term"},
            {"service": "backend", "result": "skipped_identity_mismatch"},
        ],
    }
    assert retried["status"] == "completed"
    assert retried["failure"] is None


def test_should_reject_teardown_failure_category_with_completed_teardown(
    report_validator: Draft202012Validator,
):
    from run_report import RunReportError, record_cleanup, validate_run_report

    report = record_cleanup(
        _initial_report(),
        results=[{"service": "backend", "result": "failed", "message": "stop failed"}],
        ended_at="2026-07-17T00:01:00+00:00",
    )
    report["teardown"]["status"] = "completed"

    with pytest.raises(RunReportError, match="teardown"):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report))


def test_should_reject_completed_teardown_with_accumulated_failed_result(
    report_validator: Draft202012Validator,
):
    from run_report import (
        RunReportError,
        record_cleanup,
        record_failure,
        validate_run_report,
    )

    report = record_cleanup(
        record_failure(_initial_report(), category="readiness", message="not ready"),
        results=[{"service": "backend", "result": "failed", "message": "stop failed"}],
        ended_at="2026-07-17T00:01:00+00:00",
    )
    report["teardown"]["status"] = "completed"

    with pytest.raises(RunReportError, match="teardown"):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report))


def test_should_accept_non_teardown_failure_with_completed_cleanup(
    report_validator: Draft202012Validator,
):
    from run_report import record_cleanup, record_failure

    report = record_cleanup(
        record_failure(_initial_report(), category="readiness", message="not ready"),
        results=[{"service": "frontend", "result": "stopped_term"}],
        ended_at="2026-07-17T00:01:00+00:00",
    )

    report_validator.validate(report)
    assert report["failure"]["category"] == "readiness"
    assert report["teardown"]["status"] == "completed"


def test_should_accept_ready_gate_as_teardown_target(
    report_validator: Draft202012Validator,
):
    from run_report import record_cleanup

    updated = record_cleanup(
        _initial_report(),
        results=[
            {
                "service": "ready_gate",
                "result": "failed",
                "message": "ready gate close failed",
            }
        ],
        ended_at="2026-07-17T00:01:00+00:00",
    )

    report_validator.validate(updated)
    assert updated["teardown"]["results"][0]["service"] == "ready_gate"


def test_should_reject_unknown_or_missing_report_fields():
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    missing = deepcopy(report)
    missing.pop("services")
    unknown = {**report, "environment": {"API_TOKEN": "secret"}}

    with pytest.raises(RunReportError):
        validate_run_report(missing)
    with pytest.raises(RunReportError):
        validate_run_report(unknown)


@pytest.mark.parametrize(
    ("mutate", "description"),
    [
        (lambda report: report.update(runId=42), "non-string runId"),
        (lambda report: report.update(startedAt=None), "null startedAt"),
        (
            lambda report: report.update(startedAt="not-a-date"),
            "invalid startedAt",
        ),
        (
            lambda report: report.update(startedAt="2026-07-17T00:00:00+0000"),
            "offset without colon",
        ),
        (
            lambda report: report.update(startedAt="2026-07-17T00:00:00+00:00:30"),
            "offset with seconds",
        ),
        (
            lambda report: report.update(startedAt="2026-07-17T00:00:00,5+00:00"),
            "comma fractional seconds",
        ),
        (lambda report: report.update(effectiveProfile=[]), "array effectiveProfile"),
        (
            lambda report: report["services"]["frontend"].update(mode="invented"),
            "invalid service mode",
        ),
        (
            lambda report: report["services"]["frontend"].update(source="invented"),
            "invalid service source",
        ),
    ],
)
def test_should_keep_runtime_validator_conformant_with_public_schema(
    report_validator: Draft202012Validator, mutate, description: str
):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    mutate(report)

    with pytest.raises(RunReportError, match="invalid"):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report)), description


@pytest.mark.parametrize("state", ["start_required", "failed", "stopped"])
def test_should_reject_service_states_without_report_transitions(
    report_validator: Draft202012Validator, state: str
):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    report["services"]["frontend"]["state"] = state

    with pytest.raises(RunReportError, match="state"):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda service: service.update(state="started"),
        lambda service: service.update(owned=True, state="started"),
        lambda service: service.update(
            owned=False,
            processIdentity={"pid": 41, "pgid": 41, "sessionId": 41, "startTime": 82},
        ),
        lambda service: service.update(
            owned=True,
            state="reused",
            processIdentity={"pid": 41, "pgid": 41, "sessionId": 41, "startTime": 82},
        ),
    ],
)
def test_should_keep_ownership_invariants_conformant_with_public_schema(
    report_validator: Draft202012Validator, mutate
):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    mutate(report["services"]["frontend"])

    with pytest.raises(RunReportError):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report))


def test_should_reject_external_started_owned_service_in_validator_and_schema(
    report_validator: Draft202012Validator,
):
    from run_report import RunReportError, update_service, validate_run_report

    identity = {"pid": 41, "pgid": 41, "sessionId": 41, "startTime": 82}
    report = update_service(
        _initial_report(),
        "backend",
        state="started",
        owned=True,
        process_identity=identity,
    )
    report["services"]["backend"]["source"] = "external"

    with pytest.raises(RunReportError, match="managed source"):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report))


@pytest.mark.parametrize(
    ("source", "state"),
    [
        ("managed", "external"),
        ("external", "reused"),
        ("in_process", "pending"),
        ("browser", "disabled"),
        (None, "pending"),
    ],
)
def test_should_reject_source_state_contradictions_in_validator_and_schema(
    report_validator: Draft202012Validator, source: str | None, state: str
):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    report["services"]["backend"].update(source=source, state=state)

    with pytest.raises(RunReportError, match="must agree"):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report))


@pytest.mark.parametrize("field", ["mode", "source"])
def test_should_reject_service_that_disagrees_with_effective_profile(field: str):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    replacement = "external" if field == "source" else "mock"
    report["effectiveProfile"]["dependencies"]["backend"][field] = replacement

    with pytest.raises(RunReportError, match=f"{field} must match effectiveProfile"):
        validate_run_report(report)


@pytest.mark.parametrize(
    ("service", "identity_argument", "identity"),
    [
        (
            "frontend",
            "container_identity",
            {"containerId": "frontend-container", "startedAt": "2026-07-17T00:00:00Z"},
        ),
        (
            "voicevox",
            "process_identity",
            {"pid": 41, "pgid": 41, "sessionId": 41, "startTime": 82},
        ),
    ],
)
def test_should_reject_identity_kind_that_does_not_match_service(
    service: str, identity_argument: str, identity: dict[str, object]
):
    from run_report import RunReportError, update_service

    with pytest.raises(RunReportError, match="identity"):
        update_service(
            _initial_report(),
            service,
            state="started",
            owned=True,
            **{identity_argument: identity},
        )


def test_should_not_serialize_process_environment_or_secret_values():
    report = _initial_report()
    serialized = json.dumps(report)

    assert set(report) == {
        "schemaVersion",
        "runId",
        "startedAt",
        "readyAt",
        "endedAt",
        "resolvedProfilePath",
        "orchestratorIdentity",
        "effectiveProfile",
        "phase",
        "status",
        "startSequence",
        "services",
        "testResult",
        "failure",
        "teardown",
    }
    assert "API_TOKEN" not in serialized


@pytest.mark.parametrize(
    "mutate",
    [
        lambda report: report["services"]["backend"].update(
            state="started",
            owned=True,
            processIdentity={"pid": 41, "pgid": 41, "sessionId": 41, "startTime": 82},
        ),
        lambda report: report["startSequence"].append("backend"),
    ],
)
def test_should_reject_start_sequence_that_differs_from_owned_services(
    report_validator: Draft202012Validator, mutate
):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    mutate(report)

    with pytest.raises(RunReportError, match="startSequence"):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report))


def test_should_preserve_failed_playwright_result_through_cleanup(
    report_validator: Draft202012Validator,
):
    from run_report import record_cleanup, record_test_result

    report = record_test_result(
        _initial_report(),
        status="failed",
        ended_at="2026-07-17T00:00:45+00:00",
        message="Playwright finished with status failed",
    )
    completed = record_cleanup(
        report, results=[], ended_at="2026-07-17T00:01:00+00:00"
    )

    report_validator.validate(completed)
    assert completed["testResult"]["status"] == "failed"
    assert completed["failure"]["category"] == "test"
    assert completed["status"] == "failed"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda report: report.update(
                phase="ready",
                status="ready",
                readyAt="2026-07-17T00:00:30+00:00",
                testResult={
                    "status": "failed",
                    "endedAt": "2026-07-17T00:00:45+00:00",
                    "message": "Playwright failed",
                },
            ),
            "failed testResult",
        ),
        (
            lambda report: report.update(
                status="failed",
                failure={"category": "test", "message": "Playwright failed"},
            ),
            "test failure",
        ),
    ],
)
def test_should_reject_test_result_lifecycle_contradictions_consistently_with_schema(
    report_validator: Draft202012Validator, mutate, message: str
):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    mutate(report)

    with pytest.raises(RunReportError, match=message):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report))


def test_should_preserve_prior_environment_failure_with_failed_test_result(
    report_validator: Draft202012Validator,
):
    from run_report import record_failure, record_test_result

    failed_environment = record_failure(
        _initial_report(), category="supervision", message="backend exited"
    )
    report = record_test_result(
        failed_environment,
        status="failed",
        ended_at="2026-07-17T00:00:45+00:00",
        message="Playwright failed",
    )

    report_validator.validate(report)
    assert report["status"] == "failed"
    assert report["failure"] == {
        "category": "supervision",
        "message": "backend exited",
    }
    assert report["testResult"]["status"] == "failed"


def test_should_reject_non_string_test_result_status_consistently_with_schema(
    report_validator: Draft202012Validator,
):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    report["testResult"] = {
        "status": [],
        "endedAt": "2026-07-17T00:00:45+00:00",
        "message": "invalid raw status",
    }

    with pytest.raises(RunReportError, match="testResult"):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report))


@pytest.mark.parametrize(
    ("mutate", "description"),
    [
        (lambda report: report.update(phase="complete"), "complete without endedAt"),
        (lambda report: report.update(status="failed"), "failed without failure"),
        (
            lambda report: report.update(
                failure={"category": "startup", "message": "startup failed"}
            ),
            "running with failure",
        ),
        (
            lambda report: report.update(endedAt="2026-07-17T00:01:00+00:00"),
            "ended before complete",
        ),
        (
            lambda report: report.update(
                teardown={"status": "completed", "results": []}
            ),
            "teardown completed before lifecycle",
        ),
        (lambda report: report.update(status="ready"), "ready status during verify"),
    ],
)
def test_should_reject_lifecycle_contradictions_consistently_with_schema(
    report_validator: Draft202012Validator, mutate, description: str
):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    mutate(report)

    with pytest.raises(RunReportError, match="lifecycle"):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report)), description


@pytest.mark.parametrize(
    ("mutate", "description"),
    [
        (
            lambda report: report.update(readyAt="2026-07-17T00:00:30+00:00"),
            "pre-ready phase with readyAt",
        ),
        (
            lambda report: report.update(phase="ready", status="ready", readyAt=None),
            "ready phase without readyAt",
        ),
        (
            lambda report: report.update(
                phase="complete",
                status="completed",
                endedAt="2026-07-17T00:01:00+00:00",
                teardown={"status": "completed", "results": []},
                readyAt=None,
            ),
            "completed status without readyAt",
        ),
    ],
)
def test_should_reject_ready_timestamp_state_contradictions_consistently_with_schema(
    report_validator: Draft202012Validator, mutate, description: str
):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    mutate(report)

    with pytest.raises(RunReportError, match="ready"):
        validate_run_report(report)
    assert list(report_validator.iter_errors(report)), description


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("readyAt", "2026-07-16T23:59:59+00:00", "readyAt"),
        ("endedAt", "2026-07-16T23:59:59+00:00", "endedAt"),
    ],
)
def test_should_reject_lifecycle_timestamp_before_started_at(
    field: str, value: str, message: str
):
    from run_report import RunReportError, validate_run_report

    report = _initial_report()
    report[field] = value
    if field == "endedAt":
        report.update(
            phase="complete",
            status="failed",
            failure={"category": "startup", "message": "startup failed"},
            teardown={"status": "completed", "results": []},
        )

    with pytest.raises(RunReportError, match=message):
        validate_run_report(report)


def test_should_record_playwright_result_through_atomic_cli_api(tmp_path: Path):
    from commands.test_result_command import record_playwright_result
    from run_report_store import RunReportStore

    path = tmp_path / "environment-run.json"
    RunReportStore(path).save(_initial_report())

    exit_code = record_playwright_result(
        str(path), "failed", "Playwright finished with status timedout"
    )

    report = RunReportStore(path).load()
    assert exit_code == 0
    assert report["testResult"]["status"] == "failed"
    assert report["failure"]["category"] == "test"


def test_should_serialize_competing_report_updates_without_losing_fields(
    tmp_path: Path,
):
    from run_report import record_failure, record_test_result
    from run_report_store import RunReportStore

    path = tmp_path / "environment-run.json"
    store = RunReportStore(path)
    store.save(_initial_report())
    first_transform_entered = threading.Event()
    release_first_transform = threading.Event()

    def record_supervision_failure(report: dict[str, object]) -> dict[str, object]:
        first_transform_entered.set()
        assert release_first_transform.wait(timeout=5)
        return record_failure(
            report, category="supervision", message="backend exited"
        )

    first = threading.Thread(target=lambda: store.update(record_supervision_failure))
    second = threading.Thread(
        target=lambda: store.update(
            lambda report: record_test_result(
                report,
                status="passed",
                ended_at="2026-07-17T00:00:40+00:00",
                message="Playwright passed",
            )
        )
    )

    first.start()
    assert first_transform_entered.wait(timeout=5)
    second.start()
    release_first_transform.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    report = store.load()
    assert report["failure"]["category"] == "supervision"
    assert report["testResult"]["status"] == "passed"


def test_should_replace_report_atomically_and_roundtrip_validated_state(tmp_path: Path):
    from run_report_store import RunReportStore

    path = tmp_path / "environment-run.json"
    path.write_text('{"knownGood":true}\n', encoding="utf-8")
    old_inode = path.stat().st_ino
    store = RunReportStore(path)
    report = _initial_report()

    store.save(report)
    loaded = store.load()

    assert loaded == report
    assert path.stat().st_ino != old_inode
    assert list(tmp_path.glob(f".{path.name}.*")) == []


def test_should_leave_previous_report_unchanged_when_validation_fails(tmp_path: Path):
    from run_report import RunReportError
    from run_report_store import RunReportStore

    path = tmp_path / "environment-run.json"
    original = b'{"knownGood":true}\n'
    path.write_bytes(original)
    invalid = {**_initial_report(), "status": "invented"}

    with pytest.raises(RunReportError):
        RunReportStore(path).save(invalid)

    assert path.read_bytes() == original

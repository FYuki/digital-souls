from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.environment_entrypoint_test_support import ROOT_DIR
def test_should_require_explicit_report_path_for_down(tmp_path: Path):
    result = subprocess.run(
        [str(ROOT_DIR / "environments" / "down.sh")],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "run report" in result.stderr.lower()


def test_should_only_request_orchestrator_stop_for_live_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.down_command as down_command
    from process_control import ProcessStopResult
    from run_report import create_initial_report
    from run_report_store import RunReportStore
    from tests.environment_test_support import orchestrator_identity, resolved_profile

    report_path = tmp_path / "environment-run.json"
    report = create_initial_report(
        run_id="live-down",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=resolved_profile(),
        orchestrator_identity=orchestrator_identity(),
    )
    RunReportStore(report_path).save(report)
    stop_requests: list[dict[str, int]] = []
    monkeypatch.setattr(
        down_command,
        "request_process_stop",
        lambda identity: stop_requests.append(identity.to_report())
        or ProcessStopResult("signaled"),
    )
    monkeypatch.setattr(
        down_command,
        "cleanup_environment_services",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("live down must not clean up services")
        ),
    )

    exit_code = down_command.down_environment(ROOT_DIR, str(report_path))

    assert exit_code == 0
    assert stop_requests == [orchestrator_identity()]
    assert RunReportStore(report_path).load() == report


def test_should_not_fall_back_to_direct_cleanup_when_orchestrator_signal_is_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.down_command as down_command
    from run_report import create_initial_report
    from run_report_store import RunReportStore
    from tests.environment_test_support import orchestrator_identity, resolved_profile

    report_path = tmp_path / "environment-run.json"
    report = create_initial_report(
        run_id="permission-denied-down",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=resolved_profile(),
        orchestrator_identity=orchestrator_identity(),
    )
    RunReportStore(report_path).save(report)
    cleanup_called = False

    def record_cleanup_call(*_args, **_kwargs):
        nonlocal cleanup_called
        cleanup_called = True
        return []

    monkeypatch.setattr(
        down_command,
        "request_process_stop",
        lambda identity: (_ for _ in ()).throw(PermissionError("signal denied")),
    )
    monkeypatch.setattr(
        down_command, "cleanup_environment_services", record_cleanup_call
    )

    with pytest.raises(PermissionError, match="signal denied"):
        down_command.down_environment(ROOT_DIR, str(report_path))

    assert cleanup_called is False
    assert RunReportStore(report_path).load() == report


def test_should_reject_profile_service_mismatch_before_down_stopper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.down_command as down_command
    from run_report import RunReportError, create_initial_report
    from tests.environment_test_support import orchestrator_identity, resolved_profile

    report = create_initial_report(
        run_id="contradictory-down-report",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=resolved_profile(),
        orchestrator_identity=orchestrator_identity(),
    )
    report["effectiveProfile"]["dependencies"]["backend"]["source"] = "external"
    report_path = tmp_path / "environment-run.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    cleanup_called = False

    def fail_if_cleanup_called(*_args, **_kwargs):
        nonlocal cleanup_called
        cleanup_called = True
        return []

    monkeypatch.setattr(
        down_command, "cleanup_environment_services", fail_if_cleanup_called
    )

    with pytest.raises(RunReportError, match="source must match effectiveProfile"):
        down_command.down_environment(ROOT_DIR, str(report_path))

    assert cleanup_called is False


def test_should_report_invalid_raw_test_result_without_traceback(tmp_path: Path):
    from run_report import create_pending_report
    from tests.environment_test_support import orchestrator_identity

    report_path = tmp_path / "invalid-environment-run.json"
    report = create_pending_report(
        run_id="invalid-test-result",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        orchestrator_identity=orchestrator_identity(),
    )
    report["testResult"] = {
        "status": [],
        "endedAt": "2026-07-17T00:00:45+00:00",
        "message": "invalid raw status",
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = subprocess.run(
        [str(ROOT_DIR / "environments" / "down.sh"), "--run-report", str(report_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "ERROR: invalid testResult" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "prior_cleanup_results",
    [
        [],
        [{"service": "backend", "result": "failed", "message": "TERM timed out"}],
    ],
)
def test_should_rerun_down_for_completed_stale_report(
    prior_cleanup_results: list[dict[str, object]], tmp_path: Path
):
    from run_report import (
        create_initial_report,
        record_cleanup,
        record_ready,
        update_service,
    )
    from run_report_store import RunReportStore
    from tests.environment_test_support import orchestrator_identity, resolved_profile

    report = create_initial_report(
        run_id="completed-down",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=resolved_profile(),
        orchestrator_identity=orchestrator_identity(),
    )
    report = update_service(
        report,
        "backend",
        state="started",
        owned=True,
        process_identity={
            "pid": 2_147_483_647,
            "pgid": 2_147_483_647,
            "sessionId": 2_147_483_647,
            "startTime": 1,
        },
    )
    report = record_ready(report, ready_at="2026-07-17T00:00:30+00:00")
    report = record_cleanup(
        report,
        results=prior_cleanup_results,
        ended_at="2026-07-17T00:01:00+00:00",
    )
    report_path = tmp_path / "environment-run.json"
    RunReportStore(report_path).save(report)

    result = subprocess.run(
        [str(ROOT_DIR / "environments" / "down.sh"), "--run-report", str(report_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    updated = RunReportStore(report_path).load()
    assert result.returncode == 0, result.stderr
    assert updated["phase"] == "complete"
    expected_results = [
        *prior_cleanup_results,
        {"service": "backend", "result": "skipped_identity_mismatch"},
    ]
    assert updated["teardown"]["results"] == expected_results
    assert updated["teardown"]["status"] == (
        "failed" if prior_cleanup_results else "completed"
    )
    assert updated["status"] == ("failed" if prior_cleanup_results else "completed")
    assert (updated["failure"] or {}).get("category") == (
        "teardown" if prior_cleanup_results else None
    )

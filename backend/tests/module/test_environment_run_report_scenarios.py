from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from environment_constants import DEPENDENCY_NAMES
from tests.environment_test_support import (
    profile_with_dependencies,
    single_adapter_registry,
)


class _NeverReadyOperations:
    def __init__(self) -> None:
        self.probe_calls = 0
        self.stop_calls = 0

    def verify(self, dependency, context):
        from adapters.base import Check, VerificationResult

        return VerificationResult((Check("frontend", "ready", "ready", False),))

    def prepare(self, dependency, context):
        return None

    def probe(self, dependency, timeout_seconds):
        from http_readiness import ReadinessResult

        self.probe_calls += 1
        return ReadinessResult(
            str(dependency["readinessUrl"]), 1, 0.0, "not_ready"
        )

    def start(self, dependency, environment):
        from adapters.base import ServiceStartResult

        return ServiceStartResult(
            "started",
            True,
            process_identity={"pid": 91, "pgid": 91, "sessionId": 91, "startTime": 1},
        )

    def validate_readiness(self, dependency):
        from adapters.base import ReadinessValidationResult

        return ReadinessValidationResult("ready")

    def is_running(self, service):
        return True

    def stop(self, service, grace_seconds):
        from adapters.base import StopResult

        self.stop_calls += 1
        return StopResult("stopped")


class _ExitedFrontendOperations(_NeverReadyOperations):
    def is_running(self, service):
        return False


def test_should_persist_schema_valid_readiness_timeout_from_up_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment_report_validator,
):
    import commands.up_command as up_command
    from environment_timing import EnvironmentTiming

    disabled = {"mode": "disabled", "source": None}
    profile = profile_with_dependencies(
        backend=disabled,
        ollama=disabled,
        voicevox=disabled,
        whisper=disabled,
        chroma=disabled,
    )
    adapter = _NeverReadyOperations()
    report_path = tmp_path / "environment-run.json"
    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda environment, default, report, legacy: profile,
    )
    arguments = argparse.Namespace(
        run_report=str(report_path),
        profile_report=None,
        default_profile="integration-voice",
    )

    exit_code = up_command.up_environment(
        tmp_path,
        tmp_path / ".runtime",
        arguments,
        registry=single_adapter_registry("frontend", adapter),
        timing=EnvironmentTiming(
            readiness_attempts=2,
            readiness_interval_seconds=0,
            request_timeout_seconds=0,
            supervision_interval_seconds=0,
        ),
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    environment_report_validator.validate(report)
    assert exit_code == 1
    assert adapter.probe_calls == 4
    assert adapter.stop_calls == 1
    assert report["failure"]["category"] == "readiness"
    assert report["services"]["frontend"]["readiness"]["attempts"] == 2
    assert report["services"]["frontend"]["readiness"]["result"] == "timeout"
    assert report["teardown"]["results"] == [
        {"service": "frontend", "result": "stopped"}
    ]


def test_should_persist_managed_exit_as_schema_valid_supervision_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment_report_validator,
):
    import commands.up_command as up_command
    from environment_timing import EnvironmentTiming

    disabled = {"mode": "disabled", "source": None}
    profile = profile_with_dependencies(
        backend=disabled,
        ollama=disabled,
        voicevox=disabled,
        whisper=disabled,
        chroma=disabled,
    )
    adapter = _ExitedFrontendOperations()
    report_path = tmp_path / "environment-run.json"
    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda environment, default, report, legacy: profile,
    )
    arguments = argparse.Namespace(
        run_report=str(report_path),
        profile_report=None,
        default_profile="integration-voice",
    )

    exit_code = up_command.up_environment(
        tmp_path,
        tmp_path / ".runtime",
        arguments,
        registry=single_adapter_registry("frontend", adapter),
        timing=EnvironmentTiming(
            readiness_attempts=1,
            readiness_interval_seconds=0,
            request_timeout_seconds=0,
            supervision_interval_seconds=0,
        ),
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    environment_report_validator.validate(report)
    frontend = report["services"]["frontend"]
    assert exit_code == 1
    assert adapter.probe_calls == 2
    assert adapter.stop_calls == 1
    assert report["failure"]["category"] == "supervision"
    assert report["startSequence"] == ["frontend"]
    assert frontend["state"] == "started"
    assert frontend["owned"] is True
    assert frontend["readiness"]["result"] == "not_ready"
    assert report["teardown"] == {
        "status": "completed",
        "results": [{"service": "frontend", "result": "stopped"}],
    }


def test_should_record_failed_teardown_when_voicevox_rollback_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment_report_validator
):
    import commands.up_command as up_command
    import environment_runtime
    from adapters.voicevox import VoicevoxAdapter
    from http_readiness import ReadinessResult
    from tests.environment_test_support import RecordingRunner, resolved_profile

    profile = resolved_profile()
    dependencies = {
        name: (
            profile["dependencies"][name]
            if name == "voicevox"
            else {"mode": "disabled", "source": None}
        )
        for name in DEPENDENCY_NAMES
    }
    profile["dependencies"] = dependencies
    runner = RecordingRunner(
        [
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":false,"StartedAt":"2026-07-16T00:00:00Z"}}]',
                "stderr": "",
            },
            {"returncode": 0, "stdout": "container-a\n", "stderr": ""},
            {"returncode": 1, "stdout": "", "stderr": "inspect failed"},
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":true,"StartedAt":"2026-07-17T00:00:00Z"}}]',
                "stderr": "",
            },
            {"returncode": 1, "stdout": "", "stderr": "rollback stop failed"},
            {
                "returncode": 0,
                "stdout": '[{"Id":"container-a","State":{"Running":true,"StartedAt":"2026-07-17T00:00:00Z"}}]',
                "stderr": "",
            },
            {"returncode": 0, "stdout": "container-a\n", "stderr": ""},
        ]
    )
    adapter = VoicevoxAdapter(tmp_path, runner)
    registry = single_adapter_registry("voicevox", adapter)
    report_path = tmp_path / "environment-run.json"
    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda env, default, path, legacy: profile,
    )
    monkeypatch.setattr(up_command, "create_service_registry", lambda root: registry)
    monkeypatch.setattr(
        environment_runtime,
        "verification_checks",
        lambda checked_profile, checked_registry, *, request_timeout_seconds: {
            "voicevox": {"classification": "start_required", "checks": []}
        },
    )
    monkeypatch.setattr(
        "adapters.base.probe_http",
        lambda url, timeout_seconds: ReadinessResult(url, 1, 0.001, "not_ready"),
    )
    arguments = argparse.Namespace(
        run_report=str(report_path),
        profile_report=None,
        default_profile="integration-voice",
    )

    exit_code = up_command.up_environment(tmp_path, tmp_path / ".runtime", arguments)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    environment_report_validator.validate(report)
    assert exit_code == 1
    assert report["failure"]["category"] == "startup"
    assert report["failure"]["message"] == "failed to inspect VOICEVOX container: inspect failed"
    assert report["teardown"]["status"] == "failed"
    assert report["teardown"]["results"] == [
        {
            "service": "voicevox",
            "result": "failed",
            "message": "rollback stop failed",
        },
        {"service": "voicevox", "result": "stopped"},
    ]

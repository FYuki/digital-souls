from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from environment_constants import DEPENDENCY_NAMES

from tests.environment_test_support import (
    profile_with_dependencies,
    single_adapter_registry,
    write_cached_whisper_model,
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


def test_should_route_effective_profile_to_up_service_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import commands.up_command as up_command

    from tests.environment_test_support import resolved_profile

    profile = resolved_profile("dogfood")
    captured_settings: dict[str, object] = {}

    class ExpectedStop(RuntimeError):
        pass

    def create_registry(root, runtime, **settings):
        captured_settings.update(settings)
        raise ExpectedStop("registry captured")

    monkeypatch.setenv("DS_ENVIRONMENT_ID", "test")
    monkeypatch.setenv("DS_DATA_DIR", str(tmp_path / "runtime-data"))
    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda env, default, path, legacy, runtime: profile,
    )
    monkeypatch.setattr(up_command, "create_service_registry", create_registry)
    report_dir = tmp_path / "runtime-data" / "runtime" / "test"
    arguments = argparse.Namespace(
        run_report=str(report_dir / "environment-run.json"),
        profile_report=str(report_dir / "resolved-profile.json"),
        default_profile="dogfood",
    )

    exit_code = up_command.up_environment(tmp_path, arguments)

    assert exit_code == 1
    assert captured_settings["effective_profile"] == "dogfood"

class _ExitedFrontendOperations(_NeverReadyOperations):
    def is_running(self, service):
        return False


def test_should_abort_up_during_preparation_when_whisper_inference_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment_report_validator,
    runtime_paths,
) -> None:
    import commands.up_command as up_command
    from adapters.backend import BackendAdapter
    from http_readiness import ReadinessResult

    from tests.environment_test_support import RecordingRunner

    disabled = {"mode": "disabled", "source": None}
    profile = profile_with_dependencies(
        frontend=disabled,
        ollama=disabled,
        voicevox=disabled,
        chroma=disabled,
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for launcher in ("setup-backend.sh", "start-backend.sh"):
        path = scripts / launcher
        path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        path.chmod(0o755)
    venv_bin = tmp_path / "backend" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    for executable in ("python", "uvicorn"):
        path = venv_bin / executable
        path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        path.chmod(0o755)
    snapshot = write_cached_whisper_model(
        tmp_path, "Systran/faster-whisper-medium"
    )
    missing_library = "Library libcublas.so.12 is not found or cannot be loaded"
    runner = RecordingRunner(
        [
            {"returncode": 0, "stdout": f"{snapshot}\n", "stderr": ""},
            {"returncode": 0, "stdout": "", "stderr": ""},
            {"returncode": 0, "stdout": f"{snapshot}\n", "stderr": ""},
            {"returncode": 1, "stdout": "", "stderr": missing_library},
        ]
    )
    registry = single_adapter_registry(
        "backend", BackendAdapter(tmp_path, runtime_paths, runner)
    )
    report_path = runtime_paths.runtime_report_dir / "whisper" / "environment-run.json"
    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda environment, default, report, legacy, runtime: profile,
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

    exit_code = up_command.up_environment(tmp_path, arguments, registry=registry)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    environment_report_validator.validate(report)
    assert exit_code == 1
    assert report["failure"]["category"] == "preparation"
    assert missing_library in report["failure"]["message"]
    assert report["startSequence"] == []


def test_should_persist_schema_valid_readiness_timeout_from_up_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment_report_validator,
    runtime_paths,
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
    report_path = runtime_paths.runtime_report_dir / "readiness" / "environment-run.json"
    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda environment, default, report, legacy, runtime: profile,
    )
    arguments = argparse.Namespace(
        run_report=str(report_path),
        profile_report=None,
        default_profile="integration-voice",
    )

    exit_code = up_command.up_environment(
        tmp_path,
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
    runtime_paths,
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
    report_path = runtime_paths.runtime_report_dir / "supervision" / "environment-run.json"
    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda environment, default, report, legacy, runtime: profile,
    )
    arguments = argparse.Namespace(
        run_report=str(report_path),
        profile_report=None,
        default_profile="integration-voice",
    )

    exit_code = up_command.up_environment(
        tmp_path,
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment_report_validator, runtime_paths
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
    report_path = runtime_paths.runtime_report_dir / "rollback" / "environment-run.json"
    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda env, default, path, legacy, runtime: profile,
    )
    monkeypatch.setattr(
        up_command, "create_service_registry", lambda root, runtime, **settings: registry
    )
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

    exit_code = up_command.up_environment(tmp_path, arguments)

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

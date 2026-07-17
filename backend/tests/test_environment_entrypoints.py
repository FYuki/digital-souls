from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).parent.parent.parent


def _copy_environment_runtime(tmp_path: Path) -> Path:
    environments = tmp_path / "environments"
    shutil.copytree(ROOT_DIR / "environments", environments)
    backend_app = tmp_path / "backend" / "app"
    backend_app.mkdir(parents=True)
    shutil.copy2(ROOT_DIR / "backend" / "app" / "__init__.py", backend_app / "__init__.py")
    shutil.copy2(
        ROOT_DIR / "backend" / "app" / "model_settings.py",
        backend_app / "model_settings.py",
    )
    return environments


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _wait_for_report_phase(path: Path, phase: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.01)
            continue
        if report["phase"] == phase:
            return
        time.sleep(0.01)
    pytest.fail(f"run report did not reach phase {phase}")


@pytest.mark.parametrize(
    ("wrapper_name", "expected_profile"),
    [
        ("start-all.sh", "dev"),
        ("start-voice-chat-e2e.sh", "integration-voice"),
    ],
)
def test_should_delegate_wrapper_arguments_and_default_profile_to_common_up(
    wrapper_name: str,
    expected_profile: str,
    tmp_path: Path,
):
    scripts = tmp_path / "scripts"
    environments = tmp_path / "environments"
    scripts.mkdir()
    environments.mkdir()
    wrapper = scripts / wrapper_name
    wrapper.write_text((ROOT_DIR / "scripts" / wrapper_name).read_text(encoding="utf-8"))
    wrapper.chmod(0o755)
    arguments = tmp_path / "arguments.json"
    _write_executable(
        environments / "up.sh",
        "python3 - \"$@\" <<'PY'\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(arguments)!r}, 'w'))\n"
        "raise SystemExit(23)\n"
        "PY\n",
    )

    result = subprocess.run(
        [str(wrapper), "--run-report", str(tmp_path / "run.json")],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 23
    assert json.loads(arguments.read_text(encoding="utf-8")) == [
        "--default-profile",
        expected_profile,
        "--run-report",
        str(tmp_path / "run.json"),
    ]


def test_should_delegate_voicevox_wrapper_to_single_service_adapter_cli(tmp_path: Path):
    scripts = tmp_path / "scripts"
    environments = tmp_path / "environments"
    scripts.mkdir()
    environments.mkdir()
    wrapper = scripts / "start-voicevox.sh"
    wrapper.write_text(
        (ROOT_DIR / "scripts" / "start-voicevox.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    arguments = tmp_path / "arguments.json"
    (environments / "environment_cli.py").write_text(
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(arguments)!r}, 'w'))\n"
        "raise SystemExit(23)\n",
        encoding="utf-8",
    )

    result = subprocess.run([str(wrapper), "--example", "value"])

    assert result.returncode == 23
    assert json.loads(arguments.read_text(encoding="utf-8")) == [
        "voicevox",
        "--default-profile",
        "dev",
        "--example",
        "value",
    ]


@pytest.mark.parametrize("script_name", ["up.sh", "down.sh", "verify.sh"])
def test_should_route_environment_shell_entrypoint_to_python_cli(
    script_name: str,
    tmp_path: Path,
):
    environments = tmp_path / "environments"
    environments.mkdir()
    source = ROOT_DIR / "environments" / script_name
    target = environments / script_name
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o755)
    arguments = tmp_path / "arguments.json"
    (environments / "environment_cli.py").write_text(
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(arguments)!r}, 'w'))\n"
        "raise SystemExit(31)\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(target), "--example", "value"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 31
    assert json.loads(arguments.read_text(encoding="utf-8")) == [
        script_name.removesuffix(".sh"),
        "--example",
        "value",
    ]


def test_should_allow_run_report_environment_override_through_up_cli(tmp_path: Path):
    environments = _copy_environment_runtime(tmp_path)
    report_path = tmp_path / "custom" / "environment-run.json"
    env = {
        **os.environ,
        "DS_PROFILE": "missing-profile",
        "DS_ENVIRONMENT_RUN_REPORT": str(report_path),
    }

    result = subprocess.run(
        [str(environments / "up.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["failure"]["category"] == "profile"
    assert report["status"] == "failed"


@pytest.mark.parametrize("configuration_source", ["cli", "environment"])
def test_should_reject_colliding_environment_output_paths_before_first_write(
    configuration_source: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import commands.up_command as up_command
    from profile_types import ProfileError

    collision = tmp_path / "colliding-report.json"
    monkeypatch.delenv("DS_ENVIRONMENT_RUN_REPORT", raising=False)
    monkeypatch.delenv("DS_PROFILE_REPORT", raising=False)
    if configuration_source == "environment":
        monkeypatch.setenv("DS_ENVIRONMENT_RUN_REPORT", str(collision))
        monkeypatch.setenv("DS_PROFILE_REPORT", str(collision))
        run_report = None
        profile_report = None
    else:
        run_report = str(collision)
        profile_report = str(collision)
    arguments = argparse.Namespace(
        run_report=run_report,
        profile_report=profile_report,
        default_profile="test-mocked",
    )

    with pytest.raises(ProfileError, match="output paths must be distinct"):
        up_command.up_environment(ROOT_DIR, tmp_path / ".runtime", arguments)

    assert not collision.exists()


def test_should_stop_owned_voicevox_when_single_service_readiness_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.voicevox_command as voicevox_command
    from adapters.base import Check, ServiceStartResult, StopResult, VerificationResult
    from http_readiness import ReadinessResult
    from tests.environment_test_support import resolved_profile

    class OwnedVoicevox:
        def __init__(self):
            self.stopped: list[dict[str, object]] = []

        def verify(self, dependency, context):
            return VerificationResult((Check("voicevox", "pending", "exists", False),))

        def prepare(self, dependency, context):
            return None

        def start(self, dependency, environment):
            return ServiceStartResult(
                "started",
                True,
                container_identity={"containerId": "owned", "startedAt": "now"},
            )

        def stop(self, service, grace_seconds):
            self.stopped.append(dict(service))
            return StopResult("stopped")

    adapter = OwnedVoicevox()
    monkeypatch.setattr(
        voicevox_command, "resolve_profile", lambda env, default: resolved_profile()
    )
    monkeypatch.setattr(voicevox_command, "create_service_registry", lambda root: object())
    monkeypatch.setattr(
        voicevox_command, "require_service_operations", lambda registry, name: adapter
    )
    monkeypatch.setattr(
        voicevox_command,
        "operation_context_for",
        lambda name, dependencies, registry: object(),
    )
    monkeypatch.setattr(
        voicevox_command,
        "wait_for_http",
        lambda url, **options: ReadinessResult(url, 120, 60.0, "timeout"),
    )

    with pytest.raises(RuntimeError, match="did not become ready"):
        voicevox_command.start_voicevox(tmp_path, "integration-voice")

    assert adapter.stopped == [
        {
            "state": "started",
            "owned": True,
            "containerIdentity": {"containerId": "owned", "startedAt": "now"},
        }
    ]


def test_should_preserve_voicevox_readiness_failure_when_cleanup_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.voicevox_command as voicevox_command
    from adapters.base import (
        AdapterOperationError,
        Check,
        ServiceStartResult,
        VerificationResult,
    )
    from http_readiness import ReadinessResult
    from tests.environment_test_support import resolved_profile

    class FailingCleanupVoicevox:
        def verify(self, dependency, context):
            return VerificationResult((Check("voicevox", "pending", "exists", False),))

        def prepare(self, dependency, context):
            return None

        def start(self, dependency, environment):
            return ServiceStartResult(
                "started",
                True,
                container_identity={"containerId": "owned", "startedAt": "now"},
            )

        def stop(self, service, grace_seconds):
            raise OSError("docker daemon disconnected")

    adapter = FailingCleanupVoicevox()
    monkeypatch.setattr(
        voicevox_command, "resolve_profile", lambda env, default: resolved_profile()
    )
    monkeypatch.setattr(voicevox_command, "create_service_registry", lambda root: object())
    monkeypatch.setattr(
        voicevox_command, "require_service_operations", lambda registry, name: adapter
    )
    monkeypatch.setattr(
        voicevox_command,
        "operation_context_for",
        lambda name, dependencies, registry: object(),
    )
    monkeypatch.setattr(
        voicevox_command,
        "wait_for_http",
        lambda url, **options: ReadinessResult(url, 120, 60.0, "timeout"),
    )

    with pytest.raises(AdapterOperationError) as error:
        voicevox_command.start_voicevox(tmp_path, "integration-voice")

    assert error.value.category == "readiness"
    assert str(error.value) == "VOICEVOX did not become ready"
    assert error.value.cleanup_failure is not None
    assert error.value.cleanup_failure.message == "docker daemon disconnected"


def test_should_retry_voicevox_cleanup_from_startup_error_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.voicevox_command as voicevox_command
    from adapters.base import (
        AdapterOperationError,
        Check,
        ServiceStartResult,
        StopResult,
        VerificationResult,
    )
    from tests.environment_test_support import resolved_profile

    initial_cleanup_failure = StopResult("failed", "rollback stop failed")
    ownership = ServiceStartResult(
        "started",
        True,
        container_identity={"containerId": "owned", "startedAt": "now"},
    )

    class StartupFailureVoicevox:
        def __init__(self):
            self.stopped: list[dict[str, object]] = []

        def verify(self, dependency, context):
            return VerificationResult((Check("voicevox", "pending", "exists", False),))

        def prepare(self, dependency, context):
            return None

        def start(self, dependency, environment):
            raise AdapterOperationError(
                "startup",
                "VOICEVOX startup failed",
                ownership=ownership,
                cleanup_failure=initial_cleanup_failure,
            )

        def stop(self, service, grace_seconds):
            self.stopped.append(dict(service))
            return StopResult("stopped")

    adapter = StartupFailureVoicevox()
    monkeypatch.setattr(
        voicevox_command, "resolve_profile", lambda env, default: resolved_profile()
    )
    monkeypatch.setattr(voicevox_command, "create_service_registry", lambda root: object())
    monkeypatch.setattr(
        voicevox_command, "require_service_operations", lambda registry, name: adapter
    )
    monkeypatch.setattr(
        voicevox_command,
        "operation_context_for",
        lambda name, dependencies, registry: object(),
    )

    with pytest.raises(AdapterOperationError) as error:
        voicevox_command.start_voicevox(tmp_path, "integration-voice")

    assert error.value.category == "startup"
    assert error.value.cleanup_failure is initial_cleanup_failure
    assert adapter.stopped == [
        {
            "state": "started",
            "owned": True,
            "containerIdentity": {"containerId": "owned", "startedAt": "now"},
        }
    ]


def test_should_stop_owned_voicevox_after_sigterm_during_single_service_readiness(
    tmp_path: Path,
):
    environments = _copy_environment_runtime(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ready_marker = tmp_path / "readiness-started"
    container_state = tmp_path / "voicevox-running"
    stop_log = tmp_path / "stop.log"
    _write_executable(
        bin_dir / "docker",
        "case \"$1\" in\n"
        "  inspect)\n"
        f"    if [ -f {str(container_state)!r} ]; then running=true; started=now; "
        "else running=false; started=before; fi\n"
        "    printf '[{\"Id\":\"owned\",\"State\":{\"Running\":%s,"
        "\"StartedAt\":\"%s\"}}]\\n' \"$running\" \"$started\"\n"
        "    ;;\n"
        f"  start) touch {str(container_state)!r} ;;\n"
        f"  stop) printf '%s\\n' \"$2\" > {str(stop_log)!r}; "
        f"rm -f {str(container_state)!r} ;;\n"
        "  *) exit 97 ;;\n"
        "esac\n",
    )
    (environments / "http_readiness.py").write_text(
        "import os, time\n"
        "from dataclasses import dataclass\n"
        "from pathlib import Path\n"
        "@dataclass(frozen=True)\n"
        "class ReadinessResult:\n"
        "    url: str\n"
        "    attempts: int\n"
        "    elapsed_seconds: float\n"
        "    result: str\n"
        "def probe_http(url, **options):\n"
        "    return ReadinessResult(url, 1, 0.0, 'not_ready')\n"
        "def wait_for_http(url, **options):\n"
        "    Path(os.environ['TEST_READY_MARKER']).touch()\n"
        "    while True:\n"
        "        time.sleep(0.05)\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "DS_PROFILE": "dev",
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TEST_READY_MARKER": str(ready_marker),
    }
    process = subprocess.Popen(
        [sys.executable, str(environments / "environment_cli.py"), "voicevox"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_marker.exists()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 1, (stdout, stderr)
    assert stop_log.read_text(encoding="utf-8").strip() == "owned"


def test_should_classify_managed_exit_during_readiness_as_supervision_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.up_command as up_command
    from environment_runtime import SupervisionError
    from tests.environment_test_support import resolved_profile

    class ReadinessFailureRun:
        def __init__(self, *, report, store, **_options):
            self.report = report
            self.store = store

        def verify(self):
            return None

        def prepare(self):
            return None

        def pre_probe(self):
            return {}

        def start_or_reuse(self, decisions):
            return None

        def wait_until_ready(self):
            raise SupervisionError("backend exited before readiness")

        def cleanup(self):
            return []

    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda env, default, path, legacy: resolved_profile("test-mocked"),
    )
    monkeypatch.setattr(up_command, "EnvironmentRun", ReadinessFailureRun)
    report_path = tmp_path / "environment-run.json"
    arguments = argparse.Namespace(
        run_report=str(report_path), profile_report=None, default_profile="test-mocked"
    )

    exit_code = up_command.up_environment(ROOT_DIR, tmp_path / ".runtime", arguments)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["failure"]["category"] == "supervision"


def test_should_finalize_interrupted_ready_run_when_wall_clock_moves_backward(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from tests.environment_test_support import resolved_profile

    import commands.up_command as up_command
    import run_report_timestamps
    from run_report import record_ready

    class InterruptedReadyRun:
        def __init__(self, *, report, store, **_options):
            self.report = report
            self.store = store

        def verify(self):
            return None

        def prepare(self):
            return None

        def pre_probe(self):
            return {}

        def start_or_reuse(self, decisions):
            return None

        def wait_until_ready(self):
            self.report = self.store.update(
                lambda report: record_ready(
                    report, ready_at="2026-07-17T00:00:02+00:00"
                )
            )

        def begin_supervision(self):
            return None

        def supervise(self):
            raise InterruptedError("received SIGTERM")

        def cleanup(self):
            return []

    timestamps = iter(
        (
            datetime(2026, 7, 17, 0, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 17, 0, 0, 0, tzinfo=timezone.utc),
        )
    )
    monkeypatch.setattr(
        run_report_timestamps, "_wall_clock_timestamp", lambda: next(timestamps)
    )
    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda env, default, path, legacy: resolved_profile("test-mocked"),
    )
    monkeypatch.setattr(up_command, "EnvironmentRun", InterruptedReadyRun)
    monkeypatch.setattr(
        up_command, "install_interrupt_handlers", lambda: (lambda: True, {})
    )
    monkeypatch.setattr(up_command, "restore_interrupt_handlers", lambda handlers: None)
    report_path = tmp_path / "environment-run.json"
    arguments = argparse.Namespace(
        run_report=str(report_path),
        profile_report=None,
        default_profile="test-mocked",
    )

    exit_code = up_command.up_environment(ROOT_DIR, tmp_path / ".runtime", arguments)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["phase"] == "complete"
    assert report["status"] == "completed"
    assert report["readyAt"] == "2026-07-17T00:00:02+00:00"
    assert report["endedAt"] == "2026-07-17T00:00:02+00:00"
    assert report["failure"] is None


def test_should_require_explicit_report_path_for_down(tmp_path: Path):
    result = subprocess.run(
        [str(ROOT_DIR / "environments" / "down.sh")],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "run report" in result.stderr.lower()


def test_should_reject_profile_service_mismatch_before_down_stopper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.down_command as down_command
    from run_report import RunReportError, create_initial_report
    from tests.environment_test_support import resolved_profile

    report = create_initial_report(
        run_id="contradictory-down-report",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=resolved_profile(),
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

    report_path = tmp_path / "invalid-environment-run.json"
    report = create_pending_report(
        run_id="invalid-test-result",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
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
    from tests.environment_test_support import resolved_profile

    report = create_initial_report(
        run_id="completed-down",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=resolved_profile(),
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
    assert updated["teardown"]["status"] == "completed"
    assert updated["teardown"]["results"] == [
        {"service": "backend", "result": "skipped_identity_mismatch"}
    ]


def test_should_finish_cleanup_and_report_after_additional_sigterm(
    tmp_path: Path,
):
    environments = _copy_environment_runtime(tmp_path)
    profile_path = environments / "profiles" / "test-mocked.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["dependencies"]["backend"] = {
        "mode": "real",
        "source": "managed",
        "baseUrl": "http://localhost:8000",
        "readinessPath": "/",
    }
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    stop_log = tmp_path / "stop-order.log"
    report_path = tmp_path / "environment-run.json"
    (environments / "http_readiness.py").write_text(
        "from dataclasses import dataclass\n"
        "@dataclass(frozen=True)\n"
        "class ReadinessResult:\n"
        "    url: str\n"
        "    attempts: int\n"
        "    elapsed_seconds: float\n"
        "    result: str\n"
        "    def to_report(self):\n"
        "        return {'url': self.url, 'attempts': self.attempts, "
        "'elapsedSeconds': self.elapsed_seconds, 'result': self.result}\n"
        "def probe_http(url, *, timeout_seconds):\n"
        "    return ReadinessResult(url, 1, 0.0, 'not_ready')\n"
        "def wait_for_http(url, *, assert_environment_running=None, **options):\n"
        "    if assert_environment_running is not None:\n"
        "        assert_environment_running()\n"
        "    return ReadinessResult(url, 1, 0.0, 'ready')\n",
        encoding="utf-8",
    )
    (environments / "service_registry.py").write_text(
        "from dataclasses import dataclass\n"
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "from adapters.base import (Check, OperationContext, "
        "ProcessServiceOperations, ReadinessValidationResult, StartSpecification, "
        "VerificationResult)\n"
        "@dataclass(frozen=True)\n"
        "class ServiceRegistration:\n"
        "    name: str\n"
        "    adapter: object\n"
        "    contained_by: object\n"
        "@dataclass(frozen=True)\n"
        "class ServiceRegistry:\n"
        "    services: dict\n"
        "    prepare_order: tuple\n"
        "    start_order: tuple\n"
        "@dataclass(frozen=True)\n"
        "class RuntimeServices:\n"
        "    prepare_order: tuple\n"
        "    start_order: tuple\n"
        "class FakeAdapter(ProcessServiceOperations):\n"
        "    def __init__(self, root_dir, label):\n"
        "        super().__init__(root_dir, label, None)\n"
        "    def verify(self, dependency, context):\n"
        "        return VerificationResult((Check(self.label, 'ready', 'ready', False),))\n"
        "    def prepare(self, dependency, context):\n"
        "        return None\n"
        "    def start_specification(self, dependency):\n"
        "        return StartSpecification((sys.executable, '-c', "
        "'import time; time.sleep(60)'), self.root_dir)\n"
        "    def validate_readiness(self, dependency):\n"
        "        return ReadinessValidationResult('ready')\n"
        "    def stop(self, service, grace_seconds):\n"
        "        with Path(os.environ['TEST_STOP_LOG']).open('a') as log:\n"
        "            log.write('begin:' + self.label + '\\n')\n"
        "            log.flush()\n"
        "        time.sleep(0.5)\n"
        "        result = super().stop(service, grace_seconds)\n"
        "        with Path(os.environ['TEST_STOP_LOG']).open('a') as log:\n"
        "            log.write(self.label + '\\n')\n"
        "        return result\n"
        "def create_service_registry(root_dir):\n"
        "    adapters = {name: FakeAdapter(root_dir, name) "
        "for name in ('frontend', 'backend')}\n"
        "    services = {name: ServiceRegistration(name, adapters.get(name), None) "
        "for name in ('frontend','backend','ollama','voicevox','whisper','chroma')}\n"
        "    return ServiceRegistry(services, ('backend','frontend'), ('backend','frontend'))\n"
        "def resolve_runtime_services(profile, registry):\n"
        "    managed = {name for name, dependency in profile['dependencies'].items() "
        "if dependency.get('source') == 'managed'}\n"
        "    return RuntimeServices(tuple(name for name in registry.prepare_order if name in managed), "
        "tuple(name for name in registry.start_order if name in managed))\n"
        "def operation_context_for(service, dependencies, registry):\n"
        "    return OperationContext(False, False)\n"
        "def require_service_operations(registry, service):\n"
        "    adapter = registry.services[service].adapter\n"
        "    if adapter is None:\n"
        "        raise ValueError(f'service has no lifecycle adapter: {service}')\n"
        "    return adapter\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "DS_PROFILE": "test-mocked",
        "DS_ENVIRONMENT_READY_URL": "http://127.0.0.1:0/ready",
        "TEST_STOP_LOG": str(stop_log),
    }
    process = subprocess.Popen(
        [str(environments / "up.sh"), "--run-report", str(report_path)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_report_phase(report_path, "supervise")
        process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 10
        while (
            (not stop_log.exists() or not stop_log.read_text(encoding="utf-8"))
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert stop_log.exists()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert process.returncode == 1, (stdout, stderr)
    assert stop_log.read_text(encoding="utf-8").splitlines() == [
        "begin:frontend",
        "frontend",
        "begin:backend",
        "backend",
    ]
    assert report["phase"] == "complete"
    assert report["status"] == "completed"
    assert report["endedAt"] is not None
    assert report["failure"] is None
    assert report["teardown"] == {
        "status": "completed",
        "results": [
            {"service": "frontend", "result": "stopped_term"},
            {"service": "backend", "result": "stopped_term"},
        ],
    }


def test_should_verify_without_creating_files_or_running_mutating_commands(tmp_path: Path):
    environments = _copy_environment_runtime(tmp_path)
    (environments / "http_readiness.py").write_text(
        "from dataclasses import dataclass\n"
        "@dataclass(frozen=True)\n"
        "class ReadinessResult:\n"
        "    url: str\n"
        "    attempts: int\n"
        "    elapsed_seconds: float\n"
        "    result: str\n"
        "    def to_report(self):\n"
        "        return {'url': self.url, 'attempts': self.attempts, "
        "'elapsedSeconds': self.elapsed_seconds, 'result': self.result}\n"
        "def probe_http(url, *, timeout_seconds):\n"
        "    return ReadinessResult(url, 1, 0.0, 'not_ready')\n"
        "def wait_for_http(url, **options):\n"
        "    return ReadinessResult(url, 1, 0.0, 'not_ready')\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    event_log = tmp_path / "mutations.log"
    for command in ("npm", "pip", "docker"):
        _write_executable(
            bin_dir / command,
            f'printf "%s\\n" "{command} $*" >> "{event_log}"\nexit 97\n',
        )
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DS_PROFILE": "test-mocked",
    }

    result = subprocess.run(
        [str(environments / "verify.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["services"]["frontend"]["classification"] in {
        "start_required",
        "preparation_required",
    }
    assert not event_log.exists()
    assert after == before


def test_should_return_nonzero_and_json_when_external_service_is_unreachable(
    tmp_path: Path,
):
    environments = _copy_environment_runtime(tmp_path)
    profile_path = environments / "profiles" / "test-mocked.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["dependencies"]["frontend"] = {
        "mode": "real",
        "source": "external",
        "baseUrl": "http://127.0.0.1:1",
        "readinessPath": "/",
    }
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = subprocess.run(
        [str(environments / "verify.sh")],
        env={**os.environ, "DS_PROFILE": "test-mocked"},
        capture_output=True,
        text=True,
        timeout=10,
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert payload["services"]["frontend"]["classification"] == "readiness"
    assert "external service is not ready: frontend" in result.stderr


def test_should_require_ollama_model_through_verify_entrypoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    import adapters.ollama
    import commands.verify_command as verify_command
    from http_readiness import ReadinessResult
    from service_registry import create_service_registry
    from tests.environment_test_support import resolved_profile

    profile = resolved_profile()
    dependencies = deepcopy(profile["dependencies"])
    for name in ("frontend", "backend", "voicevox", "whisper", "chroma"):
        dependencies[name] = {"mode": "disabled", "source": None}
    profile["dependencies"] = dependencies
    registry = create_service_registry(tmp_path)
    monkeypatch.setattr(verify_command, "resolve_profile", lambda env, default: profile)
    monkeypatch.setattr(verify_command, "create_service_registry", lambda root: registry)
    monkeypatch.setattr(
        "adapters.base.probe_http",
        lambda url, timeout_seconds: ReadinessResult(url, 1, 0.001, "ready"),
    )
    monkeypatch.setattr(
        adapters.ollama,
        "_fetch_json",
        lambda url: {"models": [{"name": "other:latest"}]},
    )

    from environment_verification import EnvironmentVerificationError

    with pytest.raises(EnvironmentVerificationError) as error:
        verify_command.verify_environment(tmp_path, None)

    result = json.loads(capsys.readouterr().out)
    assert error.value.category == "preparation"
    ollama = result["services"]["ollama"]
    assert ollama["classification"] == "preparation_required"
    assert ollama["readiness"]["result"] == "ready"
    assert any("gemma4:e4b" in check["message"] for check in ollama["checks"])


def test_should_require_ollama_model_for_external_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    import adapters.ollama
    import commands.verify_command as verify_command
    from http_readiness import ReadinessResult
    from service_registry import create_service_registry
    from tests.environment_test_support import resolved_profile

    profile = resolved_profile()
    dependencies = deepcopy(profile["dependencies"])
    for name in ("frontend", "backend", "voicevox", "whisper", "chroma"):
        dependencies[name] = {"mode": "disabled", "source": None}
    dependencies["ollama"] = {**dependencies["ollama"], "source": "external"}
    profile["dependencies"] = dependencies
    registry = create_service_registry(tmp_path)
    monkeypatch.setattr(verify_command, "resolve_profile", lambda env, default: profile)
    monkeypatch.setattr(verify_command, "create_service_registry", lambda root: registry)
    monkeypatch.setattr(
        "adapters.base.probe_http",
        lambda url, timeout_seconds: ReadinessResult(url, 1, 0.001, "ready"),
    )
    monkeypatch.setattr(
        adapters.ollama,
        "_fetch_json",
        lambda url: {"models": [{"name": "other:latest"}]},
    )

    from environment_verification import EnvironmentVerificationError

    with pytest.raises(EnvironmentVerificationError) as error:
        verify_command.verify_environment(tmp_path, None)

    result = json.loads(capsys.readouterr().out)["services"]["ollama"]
    assert error.value.category == "preparation"
    assert result["classification"] == "preparation_required"
    assert any("gemma4:e4b" in check["message"] for check in result["checks"])


def test_should_record_failed_teardown_when_voicevox_rollback_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import commands.up_command as up_command
    import environment_runtime
    from adapters.voicevox import VoicevoxAdapter
    from http_readiness import ReadinessResult
    from service_registry import ServiceRegistration, ServiceRegistry
    from tests.environment_test_support import DEPENDENCY_NAMES, RecordingRunner, resolved_profile

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
    registry = ServiceRegistry(
        services={
            name: ServiceRegistration(
                name,
                adapter if name == "voicevox" else None,
                "backend" if name in {"whisper", "chroma"} else None,
            )
            for name in DEPENDENCY_NAMES
        },
        prepare_order=("voicevox",),
        start_order=("voicevox",),
    )
    report_path = tmp_path / "environment-run.json"
    monkeypatch.setattr(
        up_command,
        "resolve_and_write_profile",
        lambda env, default, path, legacy: profile,
    )
    monkeypatch.setattr(
        environment_runtime, "create_service_registry", lambda root: registry
    )
    monkeypatch.setattr(
        environment_runtime,
        "verification_checks",
        lambda checked_profile, checked_registry: {
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

    exit_code = up_command.up_environment(ROOT_DIR, tmp_path / ".runtime", arguments)

    report = json.loads(report_path.read_text(encoding="utf-8"))
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


def test_should_ignore_default_environment_runtime_directory():
    runtime_report = ROOT_DIR / ".runtime" / "environments" / "run-001" / "environment-run.json"

    result = subprocess.run(
        ["git", "check-ignore", "--quiet", str(runtime_report)],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0

from __future__ import annotations

import sys
import os
import signal
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Mapping

import pytest
import tests.environment_test_support

from environment_constants import DEPENDENCY_NAMES
from environment_timing import EnvironmentTiming
from tests.environment_test_support import (
    RecordingRunner,
    single_adapter_registry,
)


class RecordingReportStore:
    def __init__(self, path: Path) -> None:
        from run_report_store import RunReportStore

        self.store = RunReportStore(path)
        self.history: list[dict[str, object]] = []

    def save(self, report: dict[str, object]) -> None:
        self.store.save(report)
        self.history.append(deepcopy(report))

    def load(self) -> dict[str, object]:
        return self.store.load()

    def update(
        self, transform: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        report = self.store.update(transform)
        self.history.append(deepcopy(report))
        return report


class FakeFrontendOperations:
    def __init__(self, forced_running: bool | None = None) -> None:
        self.forced_running = forced_running

    def verify(self, dependency, context):
        from adapters.base import Check, VerificationResult

        return VerificationResult(
            (Check("test-process", "ready", "test process", False),)
        )

    def prepare(self, dependency, context):
        return None

    def probe(self, dependency, timeout_seconds):
        from adapters.base import probe_http

        return probe_http(dependency["readinessUrl"], timeout_seconds=timeout_seconds)

    def start(self, dependency, environment):
        from adapters.base import ServiceStartResult
        from process_control import start_managed_process

        process = start_managed_process(
            label="frontend",
            command=(sys.executable, "-c", "import time; time.sleep(60)"),
            cwd=Path.cwd(),
            env=environment,
        )
        return ServiceStartResult("started", True, process.identity.to_report())

    def validate_readiness(self, dependency):
        from adapters.base import ReadinessValidationResult

        return ReadinessValidationResult("ready")

    def is_running(self, service):
        from process_control import ProcessIdentity, process_identity_matches

        if self.forced_running is not None:
            return self.forced_running
        return process_identity_matches(
            ProcessIdentity.from_report(_process_identity(service))
        )

    def stop(self, service, grace_seconds):
        from adapters.base import StopResult
        from process_control import ProcessIdentity, stop_owned_process

        result = stop_owned_process(
            ProcessIdentity.from_report(_process_identity(service)),
            grace_seconds=grace_seconds,
        )
        return StopResult(result.result)


def _process_identity(service: Mapping[str, object]) -> Mapping[str, object]:
    identity = service.get("processIdentity")
    if not isinstance(identity, dict):
        raise ValueError("test process identity is required")
    return identity


def _two_process_registry():
    from service_registry import ServiceRegistration, ServiceRegistry

    services = {
        name: ServiceRegistration(
            name,
            FakeFrontendOperations(
                forced_running=True if name == "frontend" else False
            )
            if name in {"frontend", "backend"}
            else None,
            "backend" if name in {"whisper", "chroma"} else None,
        )
        for name in DEPENDENCY_NAMES
    }
    return ServiceRegistry(
        services=services,
        prepare_order=("backend", "frontend"),
        start_order=("backend", "frontend"),
    )


def _consecutive_phases(history: list[dict[str, object]]) -> list[object]:
    phases: list[object] = []
    for report in history:
        if not phases or phases[-1] != report["phase"]:
            phases.append(report["phase"])
    return phases


def test_should_run_resolved_profile_through_ready_and_owned_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from environment_runtime import EnvironmentRun
    from http_readiness import ReadinessResult
    from profile_resolution import resolve_profile
    from run_report import create_initial_report

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    report_path = tmp_path / "environment-run.json"
    store = RecordingReportStore(report_path)
    report = create_initial_report(
        run_id="integration-run",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    store.save(report)
    monkeypatch.setattr(
        "adapters.base.probe_http",
        lambda url, timeout_seconds: ReadinessResult(url, 1, 0.001, "not_ready"),
    )

    def ready(url, **options):
        options["assert_environment_running"]()
        return ReadinessResult(url, 1, 0.002, "ready")

    monkeypatch.setattr("environment_runtime.wait_for_http", ready)
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("frontend", FakeFrontendOperations()),
    )

    run.verify()
    run.prepare()
    decisions = run.pre_probe()
    run.start_or_reuse(decisions)
    run.wait_until_ready()
    cleanup = run.cleanup()

    assert _consecutive_phases(store.history) == [
        "verify",
        "prepare",
        "pre_probe",
        "start_or_reuse",
        "readiness",
        "ready",
        "cleanup",
    ]
    assert cleanup == [{"service": "frontend", "result": "stopped_term"}]
    assert store.history[-2]["status"] == "ready"


def test_should_preserve_playwright_result_written_after_ready_gate_opens(
    tmp_path: Path,
):
    from commands.test_result_command import record_playwright_result
    from environment_runtime import EnvironmentRun
    from profile_resolution import resolve_profile
    from run_report import create_initial_report, record_ready

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    report_path = tmp_path / "environment-run.json"
    store = RecordingReportStore(report_path)
    report = record_ready(
        create_initial_report(
            run_id="report-writer-order",
            started_at="2026-07-17T00:00:00+00:00",
            resolved_profile_path=tmp_path / "resolved-profile.json",
            effective_profile=profile,
            orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
        ),
        ready_at="2026-07-17T00:00:30+00:00",
    )
    store.save(report)
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("frontend", FakeFrontendOperations()),
    )

    run.begin_supervision()
    record_playwright_result(report_path.as_posix(), "passed", "Playwright passed")
    run.cleanup()

    assert store.load()["testResult"]["status"] == "passed"


def test_should_finalize_report_and_stop_owned_service_when_ready_gate_close_fails(
    tmp_path: Path,
):
    from adapters.base import StopResult
    from environment_runtime import EnvironmentRun
    from profile_resolution import resolve_profile
    from run_report import create_initial_report, record_cleanup, record_ready, update_service

    class RecordingStopOperations(FakeFrontendOperations):
        def __init__(self) -> None:
            super().__init__()
            self.stopped = False

        def stop(self, service, grace_seconds):
            self.stopped = True
            return StopResult("stopped_term")

    class FailingReadyGate:
        def close(self) -> None:
            raise RuntimeError("ready gate close failed")

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    report = create_initial_report(
        run_id="ready-gate-close-failure",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    report = update_service(
        report,
        "frontend",
        state="started",
        owned=True,
        process_identity={"pid": 41, "pgid": 41, "sessionId": 41, "startTime": 82},
    )
    report = record_ready(report, ready_at="2026-07-17T00:00:30+00:00")
    store = RecordingReportStore(tmp_path / "environment-run.json")
    store.save(report)
    operations = RecordingStopOperations()
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("frontend", operations),
    )
    run.ready_gate = FailingReadyGate()
    run.ready_gate_open = True

    cleanup_results = run.cleanup()
    final_report = store.update(
        lambda current: record_cleanup(
            current,
            results=cleanup_results,
            ended_at="2026-07-17T00:01:00+00:00",
        )
    )

    assert operations.stopped is True
    assert cleanup_results == [
        {
            "service": "ready_gate",
            "result": "failed",
            "message": "ready gate close failed",
        },
        {"service": "frontend", "result": "stopped_term"},
    ]
    assert final_report["phase"] == "complete"
    assert final_report["endedAt"] == "2026-07-17T00:01:00+00:00"
    assert final_report["failure"]["category"] == "teardown"
    assert final_report["teardown"] == {
        "status": "failed",
        "results": cleanup_results,
    }


def test_should_stop_owned_service_when_cleanup_phase_update_fails(tmp_path: Path):
    from adapters.base import StopResult
    from environment_runtime import EnvironmentRun
    from profile_resolution import resolve_profile
    from run_report import create_initial_report, record_cleanup, update_service

    class FailingCleanupPhaseStore(RecordingReportStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.fail_next_update = True

        def update(self, transform):
            if self.fail_next_update:
                self.fail_next_update = False
                raise OSError("report filesystem unavailable")
            return super().update(transform)

    class RecordingStopOperations(FakeFrontendOperations):
        def __init__(self) -> None:
            super().__init__()
            self.stopped = False

        def stop(self, service, grace_seconds):
            self.stopped = True
            return StopResult("stopped_term")

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    report = create_initial_report(
        run_id="cleanup-report-update-failure",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    report = update_service(
        report,
        "frontend",
        state="started",
        owned=True,
        process_identity={"pid": 41, "pgid": 41, "sessionId": 41, "startTime": 82},
    )
    store = FailingCleanupPhaseStore(tmp_path / "environment-run.json")
    store.save(report)
    operations = RecordingStopOperations()
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("frontend", operations),
    )

    cleanup_results = run.cleanup()
    final_report = store.update(
        lambda current: record_cleanup(
            current,
            results=cleanup_results,
            ended_at="2026-07-17T00:01:00+00:00",
        )
    )

    assert operations.stopped is True
    assert cleanup_results == [
        {
            "service": "run_report",
            "result": "failed",
            "message": "run report update failed: report filesystem unavailable",
        },
        {"service": "frontend", "result": "stopped_term"},
    ]
    assert final_report["failure"]["category"] == "teardown"


def test_should_persist_started_ownership_before_delivering_pending_signal(tmp_path: Path):
    from adapters.base import ServiceStartResult
    from environment_signals import install_interrupt_handlers, restore_interrupt_handlers
    from environment_runtime import EnvironmentRun
    from profile_resolution import resolve_profile
    from run_report import create_initial_report

    class InterruptingOperations(FakeFrontendOperations):
        def start(self, dependency, environment):
            os.kill(os.getpid(), signal.SIGTERM)
            identity = {"pid": 41, "pgid": 41, "sessionId": 41, "startTime": 82}
            return ServiceStartResult("started", True, identity)

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    report = create_initial_report(
        run_id="signal-during-start",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    store = RecordingReportStore(tmp_path / "environment-run.json")
    store.save(report)
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("frontend", InterruptingOperations()),
    )
    _was_interrupted, previous = install_interrupt_handlers()
    try:
        with pytest.raises(InterruptedError):
            run.start_or_reuse({"frontend": "start_required"})
    finally:
        restore_interrupt_handlers(previous)

    frontend = store.load()["services"]["frontend"]
    assert frontend["state"] == "started"
    assert frontend["owned"] is True
    assert frontend["processIdentity"]["pid"] == 41


@pytest.mark.parametrize("adapter_fails", [False, True])
def test_should_persist_in_memory_ownership_in_final_report_when_start_update_fails(
    tmp_path: Path, adapter_fails: bool
):
    from adapters.base import AdapterOperationError, ServiceStartResult, StopResult
    from environment_runtime import EnvironmentRun
    from profile_resolution import resolve_profile
    from run_report import (
        create_initial_report,
        record_cleanup,
        record_failure,
        record_test_result,
    )

    identity = {"pid": 41, "pgid": 41, "sessionId": 41, "startTime": 82}

    class FailingOwnershipUpdateStore(RecordingReportStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.update_count = 0

        def update(self, transform):
            self.update_count += 1
            if self.update_count == 2:
                raise OSError("ownership report update failed")
            return super().update(transform)

    class RecordingStartOperations(FakeFrontendOperations):
        def __init__(self) -> None:
            super().__init__()
            self.stopped_service = None

        def start(self, dependency, environment):
            ownership = ServiceStartResult("started", True, identity)
            if adapter_fails:
                raise AdapterOperationError(
                    "startup",
                    "adapter failed after start",
                    ownership=ownership,
                    cleanup_failure=StopResult("failed", "adapter rollback failed"),
                )
            return ownership

        def stop(self, service, grace_seconds):
            self.stopped_service = service
            return StopResult("stopped_term")

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    report = create_initial_report(
        run_id=f"ownership-update-failure-{adapter_fails}",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    store = FailingOwnershipUpdateStore(tmp_path / "environment-run.json")
    store.save(report)
    operations = RecordingStartOperations()
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("frontend", operations),
    )

    with pytest.raises(OSError, match="ownership report update failed"):
        run.start_or_reuse({"frontend": "start_required"})
    store.update(
        lambda current: record_test_result(
            record_failure(
                current,
                category="startup",
                message="ownership report update failed",
            ),
            status="failed",
            ended_at="2026-07-17T00:00:30+00:00",
            message="Playwright failed",
        )
    )
    cleanup_results = run.cleanup()
    final_report = store.update(
        lambda current: record_cleanup(
            current,
            results=cleanup_results,
            ended_at="2026-07-17T00:01:00+00:00",
        )
    )

    assert operations.stopped_service["processIdentity"] == identity
    assert cleanup_results[-1] == {"service": "frontend", "result": "stopped_term"}
    assert final_report["services"]["frontend"] == {
        "mode": "real",
        "source": "managed",
        "state": "started",
        "owned": True,
        "processIdentity": identity,
        "containerIdentity": None,
        "readiness": None,
    }
    assert final_report["startSequence"] == ["frontend"]
    assert final_report["failure"] == {
        "category": "startup",
        "message": "ownership report update failed",
    }
    assert final_report["phase"] == "complete"
    assert final_report["status"] == "failed"
    assert final_report["testResult"] == {
        "status": "failed",
        "endedAt": "2026-07-17T00:00:30+00:00",
        "message": "Playwright failed",
    }
    assert final_report["teardown"] == {
        "status": "failed" if adapter_fails else "completed",
        "results": cleanup_results,
    }
    if adapter_fails:
        assert cleanup_results[0] == {
            "service": "frontend",
            "result": "failed",
            "message": "adapter rollback failed",
        }


def test_should_persist_external_probe_observation_when_verification_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from environment_runtime import EnvironmentRun
    from environment_verification import EnvironmentVerificationError
    from http_readiness import ReadinessResult
    from profile_resolution import resolve_profile
    from run_report import create_initial_report

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    dependencies = deepcopy(profile["dependencies"])
    dependencies["frontend"] = {
        **dependencies["frontend"],
        "source": "external",
        "baseUrl": "http://frontend.example:5173",
        "readinessUrl": "http://frontend.example:5173/",
    }
    profile = {**profile, "dependencies": dependencies}
    store = RecordingReportStore(tmp_path / "environment-run.json")
    report = create_initial_report(
        run_id="external-failure",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    store.save(report)
    monkeypatch.setattr(
        "adapters.base.probe_http",
        lambda url, timeout_seconds: ReadinessResult(url, 1, 0.125, "not_ready"),
    )
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("frontend", FakeFrontendOperations()),
    )

    with pytest.raises(EnvironmentVerificationError, match="external service is not ready"):
        run.verify()

    observation = store.load()["services"]["frontend"]["readiness"]
    assert observation == {
        "url": "http://frontend.example:5173/",
        "attempts": 1,
        "elapsedSeconds": 0.125,
        "result": "not_ready",
    }


def test_should_fail_verification_before_start_for_unpreparable_dependency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from environment_runtime import EnvironmentRun
    from environment_verification import EnvironmentVerificationError
    from profile_resolution import resolve_profile
    from run_report import create_initial_report

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    store = RecordingReportStore(tmp_path / "environment-run.json")
    report = create_initial_report(
        run_id="verification-failure",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    store.save(report)
    monkeypatch.setattr(
        "environment_runtime.verification_checks",
        lambda profile, registry, *, request_timeout_seconds: {
            "ollama": {
                "classification": "preparation_required",
                "checks": [
                    {
                        "name": "ollama-command",
                        "classification": "preparation_required",
                        "message": "Ollama command and model",
                        "canPrepare": False,
                    }
                ],
            }
        },
    )
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("frontend", FakeFrontendOperations()),
    )

    with pytest.raises(EnvironmentVerificationError) as error:
        run.verify()

    assert error.value.category == "preparation"
    assert store.load()["phase"] == "verify"


def test_should_reach_backend_prepare_when_whisper_cache_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from adapters.backend import BackendAdapter
    from environment_runtime import EnvironmentRun
    from http_readiness import ReadinessResult
    from run_report import create_initial_report
    from tests.environment_test_support import resolved_profile

    profile = resolved_profile()
    dependencies = deepcopy(profile["dependencies"])
    for name in ("frontend", "ollama", "voicevox", "chroma"):
        dependencies[name] = {"mode": "disabled", "source": None}
    profile["dependencies"] = dependencies
    runner = RecordingRunner()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for launcher in ("setup-backend.sh", "start-backend.sh"):
        path = scripts / launcher
        path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        path.chmod(0o755)
    adapter = BackendAdapter(tmp_path, runner)
    report = create_initial_report(
        run_id="whisper-prepare",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    store = RecordingReportStore(tmp_path / "environment-run.json")
    store.save(report)
    monkeypatch.setattr(
        "adapters.base.probe_http",
        lambda url, timeout_seconds: ReadinessResult(url, 1, 0.001, "not_ready"),
    )
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("backend", adapter),
    )

    run.verify()
    run.prepare()

    assert runner.calls[0] == (str(tmp_path / "scripts" / "setup-backend.sh"),)
    assert runner.calls[1][0] == str(tmp_path / "backend" / ".venv" / "bin" / "python")


def test_should_persist_ollama_observation_before_model_validation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import io
    import adapters.ollama
    from adapters.ollama import OllamaAdapter
    from environment_runtime import EnvironmentRun
    from environment_verification import EnvironmentVerificationError
    from http_readiness import ReadinessResult
    from profile_resolution import resolve_profile
    from run_report import create_initial_report

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    dependencies = deepcopy(profile["dependencies"])
    dependencies["frontend"] = {"mode": "disabled", "source": None}
    dependencies["ollama"] = {
        "mode": "real",
        "source": "managed",
        "baseUrl": "http://localhost:11434",
        "readinessPath": "/api/tags",
        "readinessUrl": "http://localhost:11434/api/tags",
    }
    profile = {**profile, "dependencies": dependencies}
    store = RecordingReportStore(tmp_path / "environment-run.json")
    report = create_initial_report(
        run_id="ollama-model-verification-failure",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    store.save(report)
    monkeypatch.setattr(
        "adapters.base.probe_http",
        lambda url, timeout_seconds: ReadinessResult(url, 1, 0.25, "ready"),
    )
    monkeypatch.setattr(
        adapters.ollama,
        "urlopen",
        lambda url, timeout: io.BytesIO(b'{"models":[{"name":"other:latest"}]}'),
    )
    adapter = OllamaAdapter(tmp_path)
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("ollama", adapter),
    )

    with pytest.raises(EnvironmentVerificationError) as error:
        run.verify()

    assert error.value.category == "preparation"
    assert store.load()["services"]["ollama"]["readiness"] == {
        "url": "http://localhost:11434/api/tags",
        "attempts": 1,
        "elapsedSeconds": 0.25,
        "result": "ready",
    }


@pytest.mark.parametrize("response", [OSError("connection refused"), b"not-json"])
def test_should_persist_ollama_observation_before_tags_request_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response: object
):
    import io
    import adapters.ollama
    from adapters.ollama import OllamaAdapter
    from environment_runtime import EnvironmentRun
    from environment_verification import EnvironmentVerificationError
    from http_readiness import ReadinessResult
    from profile_resolution import resolve_profile
    from run_report import create_initial_report

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    dependencies = deepcopy(profile["dependencies"])
    dependencies["frontend"] = {"mode": "disabled", "source": None}
    dependencies["ollama"] = {
        "mode": "real",
        "source": "managed",
        "baseUrl": "http://localhost:11434",
        "readinessPath": "/api/tags",
        "readinessUrl": "http://localhost:11434/api/tags",
    }
    profile = {**profile, "dependencies": dependencies}
    store = RecordingReportStore(tmp_path / "environment-run.json")
    report = create_initial_report(
        run_id="ollama-tags-request-failure",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    store.save(report)
    monkeypatch.setattr(
        "adapters.base.probe_http",
        lambda url, timeout_seconds: ReadinessResult(url, 1, 0.5, "ready"),
    )

    def urlopen_failure(url: str, timeout: float):
        if isinstance(response, BaseException):
            raise response
        return io.BytesIO(response)

    monkeypatch.setattr(adapters.ollama, "urlopen", urlopen_failure)
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("ollama", OllamaAdapter(tmp_path)),
    )

    with pytest.raises(EnvironmentVerificationError) as error:
        run.verify()

    assert error.value.category == "readiness"
    assert store.load()["services"]["ollama"]["readiness"] == {
        "url": "http://localhost:11434/api/tags",
        "attempts": 1,
        "elapsedSeconds": 0.5,
        "result": "ready",
    }


def test_should_detect_later_registered_process_exit_during_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from environment_runtime import EnvironmentRun, SupervisionError
    from http_readiness import ReadinessResult
    from profile_resolution import resolve_profile
    from run_report import create_initial_report, update_service

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    dependencies = deepcopy(profile["dependencies"])
    dependencies["backend"] = {
        "mode": "real",
        "source": "managed",
        "baseUrl": "http://localhost:8000",
        "readinessPath": "/",
        "readinessUrl": "http://localhost:8000/",
    }
    profile = {**profile, "dependencies": dependencies}
    report = create_initial_report(
        run_id="readiness-supervision",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    identity = {"pid": 10, "pgid": 10, "sessionId": 10, "startTime": 10}
    report = update_service(
        report, "frontend", state="started", owned=True, process_identity=identity
    )
    report = update_service(
        report, "backend", state="started", owned=True, process_identity=identity
    )
    store = RecordingReportStore(tmp_path / "environment-run.json")
    store.save(report)

    def observe_exit(url, **options):
        options["assert_environment_running"]()
        return ReadinessResult(url, 1, 0.001, "ready")

    monkeypatch.setattr("environment_runtime.wait_for_http", observe_exit)
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=_two_process_registry(),
    )

    with pytest.raises(SupervisionError, match="backend"):
        run.wait_until_ready()


def test_should_not_report_service_exit_as_failure_when_stop_is_requested_during_check(
    tmp_path: Path,
):
    from environment_runtime import EnvironmentRun
    from profile_resolution import resolve_profile
    from run_report import create_initial_report, update_service

    stop_requested = False

    class StopRaceOperations(FakeFrontendOperations):
        def is_running(self, service):
            nonlocal stop_requested
            stop_requested = True
            return False

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    report = create_initial_report(
        run_id="intentional-stop-race",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    report = update_service(
        report,
        "frontend",
        state="started",
        owned=True,
        process_identity={"pid": 10, "pgid": 10, "sessionId": 10, "startTime": 10},
    )
    store = RecordingReportStore(tmp_path / "environment-run.json")
    store.save(report)
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: stop_requested,
        registry=single_adapter_registry("frontend", StopRaceOperations()),
    )

    run.supervise()

    assert stop_requested is True


def test_should_reuse_ollama_through_runtime_without_starting_placeholder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from environment_runtime import EnvironmentRun
    from http_readiness import ReadinessResult
    from profile_resolution import resolve_profile
    from run_report import create_initial_report

    class ReusedOllamaOperations(FakeFrontendOperations):
        def start(self, dependency, environment):
            raise AssertionError("reused Ollama must not start a process")

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    dependencies = deepcopy(profile["dependencies"])
    dependencies["frontend"] = {"mode": "disabled", "source": None}
    dependencies["ollama"] = {
        "mode": "real",
        "source": "managed",
        "baseUrl": "http://localhost:11434",
        "readinessPath": "/api/tags",
        "readinessUrl": "http://localhost:11434/api/tags",
    }
    profile = {**profile, "dependencies": dependencies}
    adapter = ReusedOllamaOperations(forced_running=True)
    report = create_initial_report(
        run_id="reused-ollama",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    store = RecordingReportStore(tmp_path / "environment-run.json")
    store.save(report)
    monkeypatch.setattr(
        "adapters.base.probe_http",
        lambda url, timeout_seconds: ReadinessResult(url, 1, 0.001, "ready"),
    )
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("ollama", adapter),
    )

    decisions = run.pre_probe()
    run.start_or_reuse(decisions)

    assert store.load()["services"]["ollama"]["state"] == "reused"
    assert store.load()["startSequence"] == []


def test_should_not_invoke_docker_for_external_voicevox_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from adapters.voicevox import VoicevoxAdapter
    from environment_runtime import EnvironmentRun
    from http_readiness import ReadinessResult
    from profile_resolution import resolve_profile
    from run_report import create_initial_report

    profile = dict(resolve_profile({"DS_PROFILE": "test-mocked"}, None))
    dependencies = deepcopy(profile["dependencies"])
    dependencies["frontend"] = {"mode": "disabled", "source": None}
    dependencies["voicevox"] = {
        "mode": "real",
        "source": "external",
        "baseUrl": "http://voicevox.example:50021",
        "readinessPath": "/version",
        "readinessUrl": "http://voicevox.example:50021/version",
    }
    profile = {**profile, "dependencies": dependencies}
    runner = RecordingRunner()
    adapter = VoicevoxAdapter(tmp_path, runner)
    report = create_initial_report(
        run_id="external-voicevox",
        started_at="2026-07-17T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
    )
    store = RecordingReportStore(tmp_path / "environment-run.json")
    store.save(report)
    ready = lambda url, timeout_seconds: ReadinessResult(url, 1, 0.001, "ready")
    monkeypatch.setattr("adapters.base.probe_http", ready)
    monkeypatch.setattr(
        "environment_runtime.wait_for_http",
        lambda url, **options: ReadinessResult(url, 1, 0.001, "ready"),
    )
    run = EnvironmentRun(
        profile=profile,
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=report,
        timing=EnvironmentTiming(),
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=single_adapter_registry("voicevox", adapter),
    )

    run.verify()
    decisions = run.pre_probe()
    run.start_or_reuse(decisions)
    run.wait_until_ready()

    assert runner.calls == []
    assert store.load()["services"]["voicevox"]["state"] == "external"

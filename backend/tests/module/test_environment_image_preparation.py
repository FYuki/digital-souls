"""両imageの準備と依存確認を終える前にアプリを起動しない。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import tests.environment_test_support
from adapters.base import AdapterOperationError
from environment_runtime import EnvironmentRun
from environment_timing import EnvironmentTiming
from http_readiness import ReadinessResult
from profile_resolution import resolve_profile
from run_report import create_initial_report
from run_report_store import RunReportStore
from service_registry import create_service_registry
from tests.environment_test_support import resolved_runtime_paths


class ComposeRunner:
    def __init__(self, events, *, failing_build=None):
        self.events = events
        self.running = set()
        self.failing_build = failing_build

    def run(self, command, cwd):
        if command[:2] == ("docker", "inspect"):
            name = command[2]
            if name not in self.running:
                return {"returncode": 1, "stdout": "", "stderr": ""}
            return {"returncode": 0, "stderr": "", "stdout": json.dumps([{
                "Id": ("a" if name.endswith("backend") else "b") * 64,
                "State": {"Running": True, "StartedAt": "2026-09-19T00:00:00Z"},
            }])}
        if command[-2] == "build":
            name = command[-1]
            self.events.append("build:" + name)
            return {"returncode": int(name == self.failing_build), "stdout": "", "stderr": ""}
        if "up" in command:
            name = command[-1]
            self.events.append("start:" + name)
            self.running.add("digital-souls-test-" + name)
        return {"returncode": 0, "stdout": "", "stderr": ""}


def create_run(tmp_path, monkeypatch, *, failing_build=None):
    events = []
    paths = resolved_runtime_paths(tmp_path)
    profile = dict(resolve_profile({"DS_PROFILE": "integration-voice"}, None, paths))
    profile_path = tmp_path / "resolved-profile.json"
    profile_path.write_text(json.dumps(profile))
    store = RunReportStore(tmp_path / "environment-run.json")
    report = create_initial_report(
        run_id="image-order", started_at="2026-09-19T00:00:00+00:00",
        resolved_profile_path=profile_path, effective_profile=profile,
        orchestrator_identity=tests.environment_test_support.orchestrator_identity(),
        runtime=profile["runtime"],
    )
    store.save(report)
    runner = ComposeRunner(events, failing_build=failing_build)
    registry = create_service_registry(tmp_path, paths, runner, effective_profile="integration-voice")

    def probe(url, *, timeout_seconds):
        events.append("probe")
        return ReadinessResult(url, 1, 0.001, "not_ready" if url in {
            profile["dependencies"][name]["readinessUrl"] for name in ("backend", "frontend")
        } else "ready")

    monkeypatch.setattr("adapters.base.probe_http", probe)
    monkeypatch.setattr("adapters.livekit.probe_http", probe)
    # 共有serviceの有無で単体の起動順序検証が成功しないよう、実通信を拒否する。
    def unexpected_connection(*args, **kwargs):
        pytest.fail("image preparation tests must not contact external services")
    monkeypatch.setattr("socket.create_connection", unexpected_connection)
    run = EnvironmentRun(
        profile=profile, profile_path=profile_path, store=store, report=report,
        timing=EnvironmentTiming(), ready_gate=profile["readyGate"],
        was_interrupted=lambda: False, registry=registry,
    )
    return run, events, runner


def test_builds_both_images_before_any_probe_or_application_start(tmp_path, monkeypatch):
    run, events, _ = create_run(tmp_path, monkeypatch)
    run.prepare()
    assert events == ["build:backend", "build:frontend"]
    decisions = run.pre_probe()
    assert events[2:] and set(events[2:]) == {"probe"}
    run.start_or_reuse(decisions)
    assert events[-2:] == ["start:backend", "start:frontend"]
    assert events.count("build:backend") == events.count("build:frontend") == 1


def test_failed_frontend_build_starts_no_backend(tmp_path, monkeypatch):
    run, events, _ = create_run(tmp_path, monkeypatch, failing_build="frontend")
    with pytest.raises(AdapterOperationError, match="image preparation failed"):
        run.prepare()
    assert events == ["build:backend", "build:frontend"]
    assert run.report["startSequence"] == []


def test_changed_image_after_preparation_is_rejected_without_build_or_start(tmp_path, monkeypatch):
    run, events, _ = create_run(tmp_path, monkeypatch)
    run.prepare()
    decisions = run.pre_probe()
    before = list(events)
    monkeypatch.setenv("DS_BACKEND_IMAGE", "unprepared-image:changed")
    with pytest.raises(AdapterOperationError, match="environment changed after image preparation"):
        run.start_or_reuse(decisions)
    assert events == before


def test_existing_container_is_preserved_before_any_build(tmp_path, monkeypatch):
    run, events, runner = create_run(tmp_path, monkeypatch)
    runner.running.add("digital-souls-test-backend")
    with pytest.raises(AdapterOperationError, match="unowned backend container"):
        run.prepare()
    assert events == []
    assert runner.running == {"digital-souls-test-backend"}


def test_verify_does_not_build_even_with_preparation_environment(tmp_path, monkeypatch):
    from adapters.base import OperationContext

    run, events, _ = create_run(tmp_path, monkeypatch)
    context = OperationContext(False, False, run._service_environment())
    for name in ("backend", "frontend"):
        run.registry.services[name].adapter.verify(run.dependencies[name], context)
    assert events == []
    assert "DS_PROFILE" not in repr(context)

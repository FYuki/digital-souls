from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tests.environment_test_support import DEPENDENCY_NAMES, resolved_profile


def test_should_register_adapters_and_dependency_order_in_one_registry(tmp_path: Path):
    from service_registry import create_service_registry

    registry = create_service_registry(tmp_path)

    assert set(registry.services) == set(DEPENDENCY_NAMES)
    assert registry.prepare_order == ("backend", "frontend")
    assert registry.start_order == ("ollama", "voicevox", "backend", "frontend")
    assert registry.services["whisper"].contained_by == "backend"
    assert registry.services["chroma"].contained_by == "backend"


def test_should_isolate_and_freeze_registry_services(tmp_path: Path):
    from service_registry import ServiceRegistration, ServiceRegistry

    services = {
        "frontend": ServiceRegistration("frontend", None, None),
    }
    registry = ServiceRegistry(services, (), ())

    services["backend"] = ServiceRegistration("backend", None, None)

    assert set(registry.services) == {"frontend"}
    with pytest.raises(TypeError):
        registry.services["backend"] = ServiceRegistration("backend", None, None)
    with pytest.raises(AttributeError):
        registry.services.pop("frontend")


def test_should_select_only_enabled_runtime_services(tmp_path: Path):
    from service_registry import create_service_registry, resolve_runtime_services

    profile = resolved_profile("test-mocked")
    profile["dependencies"] = {
        "frontend": resolved_profile()["dependencies"]["frontend"],
        "backend": {"mode": "mock", "source": "browser"},
        "ollama": {"mode": "disabled", "source": None},
        "voicevox": {"mode": "disabled", "source": None},
        "whisper": {"mode": "disabled", "source": None},
        "chroma": {"mode": "disabled", "source": None},
    }

    runtime = resolve_runtime_services(profile, create_service_registry(tmp_path))

    assert runtime.start_order == ("frontend",)


def test_should_derive_backend_operation_context_from_registry_containment(tmp_path: Path):
    from service_registry import (
        ServiceRegistration,
        ServiceRegistry,
        create_service_registry,
        operation_context_for,
    )

    profile = resolved_profile()
    original = create_service_registry(tmp_path)
    whisper = original.services["whisper"]
    services = {
        **original.services,
        "whisper": ServiceRegistration(whisper.name, whisper.adapter, None),
    }
    registry = ServiceRegistry(services, original.prepare_order, original.start_order)

    context = operation_context_for("backend", profile["dependencies"], registry)

    assert context.whisper_enabled is False
    assert context.chroma_enabled is False


def test_should_cleanup_only_owned_started_services_in_reverse_actual_order():
    from orchestrator import cleanup_owned_services

    calls: list[str] = []
    report = {
        "startSequence": ["ollama", "voicevox", "backend", "frontend"],
        "services": {
            "ollama": {"source": "managed", "state": "reused", "owned": False},
            "voicevox": {"source": "managed", "state": "started", "owned": True},
            "backend": {"source": "managed", "state": "started", "owned": True},
            "frontend": {"source": "managed", "state": "started", "owned": True},
        },
    }
    stoppers = {
        name: (lambda service=name: calls.append(service) or {"result": "stopped"})
        for name in ("ollama", "voicevox", "backend", "frontend")
    }

    results = cleanup_owned_services(report, stoppers)

    assert calls == ["frontend", "backend", "voicevox"]
    assert [result["service"] for result in results] == calls


def test_should_continue_cleanup_and_aggregate_individual_failures():
    from orchestrator import cleanup_owned_services

    calls: list[str] = []
    report = {
        "startSequence": ["ollama", "backend", "frontend"],
        "services": {
            name: {"source": "managed", "state": "started", "owned": True}
            for name in ("ollama", "backend", "frontend")
        },
    }

    def fail_backend():
        calls.append("backend")
        raise RuntimeError("TERM failed")

    stoppers = {
        "frontend": lambda: calls.append("frontend") or {"result": "stopped"},
        "backend": fail_backend,
        "ollama": lambda: calls.append("ollama") or {"result": "stopped"},
    }

    results = cleanup_owned_services(report, stoppers)

    assert calls == ["frontend", "backend", "ollama"]
    assert [result["result"] for result in results] == ["stopped", "failed", "stopped"]


def test_should_not_cleanup_external_service_with_forged_ownership():
    from orchestrator import cleanup_owned_services

    called = False

    def stop_external():
        nonlocal called
        called = True
        return {"result": "stopped"}

    report = {
        "startSequence": ["backend"],
        "services": {
            "backend": {"source": "external", "state": "started", "owned": True}
        },
    }

    assert cleanup_owned_services(report, {"backend": stop_external}) == []
    assert called is False


def test_should_build_identity_sensitive_cleanup_from_registry_once(tmp_path: Path):
    from adapters.base import StopResult
    from environment_cleanup import cleanup_environment_services
    from service_registry import ServiceRegistration, ServiceRegistry

    class RecordingAdapter:
        def __init__(self):
            self.calls: list[tuple[dict[str, object], float]] = []

        def stop(self, service, grace_seconds):
            self.calls.append((dict(service), grace_seconds))
            return StopResult("stopped")

    adapter = RecordingAdapter()
    identity = {"pid": 41, "pgid": 41, "sessionId": 41, "startTime": 82}
    report = {
        "startSequence": ["backend"],
        "services": {
            "backend": {
                "source": "managed",
                "state": "started",
                "owned": True,
                "processIdentity": identity,
            }
        },
    }
    registry = ServiceRegistry(
        {"backend": ServiceRegistration("backend", adapter, None)}, (), ()
    )

    results = cleanup_environment_services(report, registry)

    assert results == [{"service": "backend", "result": "stopped"}]
    assert adapter.calls == [(report["services"]["backend"], 5.0)]


@pytest.mark.parametrize(
    ("phase", "expected_category"),
    [
        ("resolve", "profile"),
        ("verify", "preparation"),
        ("prepare", "preparation"),
        ("start", "startup"),
        ("readiness", "readiness"),
        ("supervise", "supervision"),
    ],
)
def test_should_classify_lifecycle_failure_by_phase(phase: str, expected_category: str):
    from orchestrator import classify_failure

    assert classify_failure(phase)["category"] == expected_category


def test_should_not_mark_environment_ready_until_every_active_http_service_is_ready():
    from orchestrator import readiness_complete

    observations = {
        "frontend": {"result": "ready"},
        "backend": {"result": "ready"},
        "ollama": {"result": "ready"},
        "voicevox": {"result": "timeout"},
    }

    assert readiness_complete(resolved_profile(), observations) is False


def test_should_mark_environment_ready_when_every_active_http_service_is_ready():
    from orchestrator import readiness_complete

    observations = {
        name: {"result": "ready"}
        for name in ("frontend", "backend", "ollama", "voicevox")
    }

    assert readiness_complete(resolved_profile(), observations) is True


def test_should_preprobe_managed_service_into_reused_state():
    from orchestrator import classify_preprobe

    result = classify_preprobe(
        resolved_profile()["dependencies"]["backend"],
        {"result": "ready", "attempts": 1, "elapsedSeconds": 0.001},
    )

    assert result.state == "reused"


def test_should_preprobe_stopped_managed_service_into_start_required_state():
    from orchestrator import classify_preprobe

    result = classify_preprobe(
        resolved_profile()["dependencies"]["backend"],
        {"result": "not_ready", "attempts": 1, "elapsedSeconds": 0.001},
    )

    assert result.state == "start_required"


def test_should_classify_external_probe_failure_as_readiness_without_starting():
    from orchestrator import classify_preprobe

    dependency = deepcopy(resolved_profile()["dependencies"]["ollama"])
    dependency["source"] = "external"
    dependency["baseUrl"] = "http://ollama.example:11434"
    dependency["readinessUrl"] = "http://ollama.example:11434/api/tags"

    result = classify_preprobe(
        dependency,
        {"result": "not_ready", "attempts": 1, "elapsedSeconds": 0.001},
    )

    assert result.state == "external"
    assert result.failure_category == "readiness"


@pytest.mark.parametrize(
    ("source", "prerequisites_ready", "probe_result", "expected"),
    [
        ("managed", True, "not_ready", "start_required"),
        ("managed", False, "not_ready", "preparation_required"),
        ("managed", True, "ready", "ready"),
        ("external", True, "not_ready", "readiness"),
        ("managed", True, "pending", "pending"),
    ],
)
def test_should_classify_verify_result_without_mutating_environment(
    source: str,
    prerequisites_ready: bool,
    probe_result: str,
    expected: str,
):
    from orchestrator import classify_verification

    result = classify_verification(
        source=source,
        prerequisites_ready=prerequisites_ready,
        probe_result=probe_result,
    )

    assert result == expected


def test_should_keep_profile_free_of_runtime_identity_and_start_sequence():
    profile = resolved_profile()
    serialized_keys = set(profile)
    dependency_keys = {
        key
        for dependency in profile["dependencies"].values()
        for key in dependency
    }

    assert "startSequence" not in serialized_keys
    assert {"pid", "pgid", "containerId", "startTime"}.isdisjoint(dependency_keys)

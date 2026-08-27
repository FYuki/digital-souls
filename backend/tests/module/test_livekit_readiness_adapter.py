from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest


def _livekit_adapter_module(contract: str) -> ModuleType:
    module_name = "adapters.livekit"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name is None or not (
            error.name == module_name or module_name.startswith(f"{error.name}.")
        ):
            raise
    pytest.fail(f"{module_name} must implement {contract}")


def test_livekit_adapter_reports_readiness_without_managed_lifecycle_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _livekit_adapter_module("readiness-only external endpoint checks")
    from http_readiness import ReadinessResult
    from profile_resolution import resolve_dependencies

    profile = json.loads(
        (Path(__file__).resolve().parents[3] / "environments" / "profiles" / "dev.json").read_text(
            encoding="utf-8"
        )
    )
    dependency = resolve_dependencies(profile["dependencies"]).get("livekit")
    assert dependency is not None

    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(
        module,
        "probe_http",
        lambda url, timeout_seconds: (
            calls.append((url, timeout_seconds))
            or ReadinessResult(url, 1, 0.01, "ready")
        ),
    )
    adapter = module.LiveKitExternalOperations()

    result = adapter.probe(dependency, 2.0)

    assert result.result == "ready"
    assert calls == [(dependency["readinessUrl"], 2.0)]
    assert all(not hasattr(adapter, operation) for operation in ("start", "stop", "restart"))


def test_livekit_readiness_failure_keeps_the_public_endpoint_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _livekit_adapter_module("diagnostic readiness failure results")
    from http_readiness import ReadinessResult
    from profile_resolution import resolve_dependencies

    profile = json.loads(
        (Path(__file__).resolve().parents[3] / "environments" / "profiles" / "dogfood.json").read_text(
            encoding="utf-8"
        )
    )
    dependency = resolve_dependencies(profile["dependencies"]).get("livekit")
    assert dependency is not None

    secret = "LIVEKIT_SECRET_SENTINEL"
    monkeypatch.setattr(
        module,
        "probe_http",
        lambda url, timeout_seconds: ReadinessResult(
            url, 1, timeout_seconds, "not_ready"
        ),
    )
    adapter = module.LiveKitExternalOperations()

    result = adapter.probe(dependency, 2.0)

    assert result.result == "not_ready"
    assert result.url == dependency["readinessUrl"]
    assert secret not in str(result)

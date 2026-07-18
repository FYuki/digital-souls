from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from tests.environment_entrypoint_test_support import (
    copy_environment_runtime as _copy_environment_runtime,
)
def test_should_verify_without_creating_files_or_running_mutating_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    import commands.verify_command as verify_command
    from adapters.base import Check, ReadinessValidationResult, VerificationResult
    from http_readiness import ReadinessResult
    from service_registry import ServiceRegistration, ServiceRegistry
    from tests.environment_test_support import DEPENDENCY_NAMES, profile_with_dependencies

    class NonMutatingOperations:
        def verify(self, dependency, context):
            return VerificationResult(
                (Check("frontend", "preparation_required", "dependencies", True),)
            )

        def probe(self, dependency, timeout_seconds):
            return ReadinessResult(
                str(dependency["readinessUrl"]), 1, 0.0, "not_ready"
            )

        def prepare(self, dependency, context):
            raise AssertionError("verify must not prepare services")

        def start(self, dependency, environment):
            raise AssertionError("verify must not start services")

        def validate_readiness(self, dependency):
            return ReadinessValidationResult("ready")

        def is_running(self, service):
            return True

        def stop(self, service, grace_seconds):
            raise AssertionError("verify must not stop services")

    disabled = {"mode": "disabled", "source": None}
    profile = profile_with_dependencies(
        backend=disabled,
        ollama=disabled,
        voicevox=disabled,
        whisper=disabled,
        chroma=disabled,
    )
    operations = NonMutatingOperations()
    registry = ServiceRegistry(
        services={
            name: ServiceRegistration(
                name,
                operations if name == "frontend" else None,
                "backend" if name in {"whisper", "chroma"} else None,
            )
            for name in DEPENDENCY_NAMES
        },
        prepare_order=("frontend",),
        start_order=("frontend",),
    )
    monkeypatch.setattr(
        verify_command, "resolve_profile", lambda environment, default: profile
    )
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    exit_code = verify_command.verify_environment(
        tmp_path, "test-mocked", registry=registry
    )

    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result["services"]["frontend"]["classification"] == "preparation_required"
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

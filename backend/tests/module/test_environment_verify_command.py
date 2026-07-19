from __future__ import annotations

import json
import os
import stat
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from tests.environment_entrypoint_test_support import (
    copy_environment_runtime as _copy_environment_runtime,
    write_executable,
)


_SUCCESS_PROFILE = "test-verify-safe"
_SUCCESS_CLASSIFICATIONS = {
    "frontend": "disabled",
    "backend": "browser",
    "ollama": "disabled",
    "voicevox": "disabled",
    "whisper": "disabled",
    "chroma": "disabled",
}


def _write_success_profile(environments: Path) -> None:
    disabled = {"mode": "disabled", "source": None}
    profile = {
        "schemaVersion": 1,
        "name": _SUCCESS_PROFILE,
        "description": "外部サービスを使わないverify entrypointテスト",
        "dependencies": {
            "frontend": disabled,
            "backend": {"mode": "mock", "source": "browser"},
            "ollama": disabled,
            "voicevox": disabled,
            "whisper": disabled,
            "chroma": disabled,
        },
    }
    profile_path = environments / "profiles" / f"{_SUCCESS_PROFILE}.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")


def _run_successful_verify(
    environments: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(environments / "verify.sh"),
            "--default-profile",
            _SUCCESS_PROFILE,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, int, bytes]]:
    snapshot: dict[str, tuple[int, int, int, bytes]] = {}
    for path in root.rglob("*"):
        metadata = path.lstat()
        content = path.read_bytes() if path.is_file() else b""
        snapshot[str(path.relative_to(root))] = (
            stat.S_IFMT(metadata.st_mode),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_mtime_ns,
            content,
        )
    return snapshot


def test_should_detect_file_timestamp_changes_in_tree_snapshot(tmp_path: Path):
    target = tmp_path / "unchanged-content.txt"
    target.write_text("unchanged", encoding="utf-8")
    before = _tree_snapshot(tmp_path)
    metadata = target.stat()

    os.utime(
        target,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
    )

    assert target.read_text(encoding="utf-8") == "unchanged"
    assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(metadata.st_mode)
    assert _tree_snapshot(tmp_path) != before


def test_should_select_cli_default_profile_through_verify_entrypoint(tmp_path: Path):
    environments = _copy_environment_runtime(tmp_path)
    _write_success_profile(environments)

    result = _run_successful_verify(
        environments,
        {"PATH": os.environ["PATH"]},
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["effectiveProfile"] == _SUCCESS_PROFILE
    assert {
        name: service["classification"]
        for name, service in payload["services"].items()
    } == _SUCCESS_CLASSIFICATIONS


def test_should_not_run_mutating_commands_or_change_files_during_verify(
    tmp_path: Path,
):
    environments = _copy_environment_runtime(tmp_path)
    _write_success_profile(environments)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "mutating-command.log"
    for command in ("npm", "pip", "pip3", "docker"):
        write_executable(
            bin_dir / command,
            f'printf "%s\\n" "{command} $*" >> "{command_log}"\nexit 97\n',
        )
    before = _tree_snapshot(tmp_path)

    result = _run_successful_verify(
        environments,
        {"PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    assert not command_log.exists()
    assert _tree_snapshot(tmp_path) == before


def test_should_verify_without_creating_files_or_running_mutating_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    import commands.verify_command as verify_command
    from adapters.base import Check, ReadinessValidationResult, VerificationResult
    from http_readiness import ReadinessResult
    from tests.environment_test_support import (
        profile_with_dependencies,
        single_adapter_registry,
    )

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
    registry = single_adapter_registry("frontend", operations)
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

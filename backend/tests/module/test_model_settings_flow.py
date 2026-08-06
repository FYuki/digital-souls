import json
import os
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.model_settings import MODEL_ENVIRONMENT_KEYS
from tests.environment_test_support import (
    RecordingRunner,
    orchestrator_identity,
    resolved_profile,
    resolved_runtime_paths,
)


MODEL_ENVIRONMENT = {
    "OLLAMA_CHAT_MODEL": "profile-chat:12b",
    "WHISPER_MODEL": "large-v3",
    "OLLAMA_CONTEXT_TOKENS": "12288",
    "OLLAMA_RESPONSE_RESERVE_TOKENS": "1536",
    "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": "6",
    "CONVERSATION_HISTORY_TOKEN_LIMIT": "3000",
    "USER_INPUT_TOKEN_LIMIT": "1000",
    "LLM_CONTEXT_TOKEN_LIMIT": "16384",
}


def _resolve_profile(environment: dict[str, str], root_dir: Path = Path("/test/repository")):
    from profile_resolution import resolve_profile

    return resolve_profile(environment, None, resolved_runtime_paths(root_dir))


def test_should_resolve_profile_overrides_into_canonical_child_environment() -> None:
    report = _resolve_profile({"DS_PROFILE": "integration-voice", **MODEL_ENVIRONMENT})

    derived = report["derivedEnvironment"]
    assert {key: derived[key] for key in MODEL_ENVIRONMENT} == MODEL_ENVIRONMENT
    assert derived["ASSISTANT_MAX_GENERATION_TOKENS"] == "1536"


def test_should_emit_model_defaults_for_profile_when_overrides_are_absent() -> None:
    report = _resolve_profile({"DS_PROFILE": "integration-text"})

    derived = report["derivedEnvironment"]
    assert derived["OLLAMA_CHAT_MODEL"] == "gemma4:e4b"
    assert derived["WHISPER_MODEL"] == "medium"
    assert derived["OLLAMA_CONTEXT_TOKENS"] == "8192"
    assert derived["OLLAMA_RESPONSE_RESERVE_TOKENS"] == "1024"


def test_should_export_all_model_keys_and_unset_them_when_switching_to_mock_backend(
    tmp_path: Path,
) -> None:
    real_report = tmp_path / "real-profile.json"
    mock_report = tmp_path / "mock-profile.json"
    real_report.write_text(
        json.dumps(
            _resolve_profile({"DS_PROFILE": "integration-text", **MODEL_ENVIRONMENT})
        ),
        encoding="utf-8",
    )
    mock_report.write_text(
        json.dumps(
            _resolve_profile({"DS_PROFILE": "test-mocked"})
        ),
        encoding="utf-8",
    )
    key_list = " ".join(MODEL_ENVIRONMENT_KEYS)
    command = (
        'source scripts/lib/profile.sh\n'
        f'DS_PROFILE_REPORT={real_report!s}\n'
        'export DS_PROFILE_REPORT\n'
        'profile_export_derived_environment\n'
        f'for key in {key_list}; do printf "real:%s=%s\\n" "$key" "${{!key-unset}}"; done\n'
        f'DS_PROFILE_REPORT={mock_report!s}\n'
        'export DS_PROFILE_REPORT\n'
        'profile_export_derived_environment\n'
        f'for key in {key_list}; do\n'
        '  if [ "${!key+x}" = "x" ]; then printf "%s=set\\n" "$key"; '
        'else printf "%s=unset\\n" "$key"; fi\n'
        'done\n'
    )

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=Path(__file__).parent.parent.parent.parent,
        env=dict(os.environ),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    expected_real_environment = _resolve_profile(
        {"DS_PROFILE": "integration-text", **MODEL_ENVIRONMENT}
    )["derivedEnvironment"]
    assert result.stdout.splitlines() == [
        *[
            f"real:{key}={expected_real_environment[key]}"
            for key in MODEL_ENVIRONMENT_KEYS
        ],
        *[f"{key}=unset" for key in MODEL_ENVIRONMENT_KEYS],
    ]


def test_should_reject_invalid_model_settings_before_writing_resolved_values() -> None:
    from profile_types import ProfileError

    with pytest.raises(ProfileError) as exc_info:
        _resolve_profile(
            {
                "DS_PROFILE": "integration-text",
                "OLLAMA_CONTEXT_TOKENS": "1024",
                "OLLAMA_RESPONSE_RESERVE_TOKENS": "1024",
            },
        )

    message = str(exc_info.value)
    assert "OLLAMA_CONTEXT_TOKENS" in message
    assert "OLLAMA_RESPONSE_RESERVE_TOKENS" in message


def test_should_route_profile_ollama_model_to_readiness_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from adapters import ollama
    from service_registry import create_service_registry, require_service_operations

    monkeypatch.setattr(
        ollama,
        "_fetch_json",
        lambda _url: {"models": [{"name": MODEL_ENVIRONMENT["OLLAMA_CHAT_MODEL"]}]},
    )
    runtime_paths = resolved_runtime_paths(tmp_path)
    report = _resolve_profile(
        {"DS_PROFILE": "integration-voice", **MODEL_ENVIRONMENT}, tmp_path
    )
    derived = report["derivedEnvironment"]
    registry = create_service_registry(
        tmp_path,
        runtime_paths,
        ollama_model_name=derived["OLLAMA_CHAT_MODEL"],
        whisper_model_name=derived["WHISPER_MODEL"],
    )

    result = require_service_operations(registry, "ollama").validate_readiness(
        resolved_profile()["dependencies"]["ollama"]
    )

    assert result.classification == "ready"
    assert result.message is None


def test_should_route_profile_whisper_model_to_cache_check(tmp_path: Path) -> None:
    from adapters.base import OperationContext
    from service_registry import create_service_registry, require_service_operations

    snapshot = (
        tmp_path
        / "runtime-data"
        / "cache"
        / "huggingface"
        / "hub"
        / "models--Systran--faster-whisper-large-v3"
        / "snapshots"
        / "revision"
    )
    snapshot.mkdir(parents=True)
    refs = snapshot.parent.parent / "refs"
    refs.mkdir()
    (refs / "main").write_text("revision", encoding="utf-8")
    for artifact in (
        "config.json",
        "model.bin",
        "preprocessor_config.json",
        "tokenizer.json",
        "vocabulary.json",
    ):
        (snapshot / artifact).write_text("fixture", encoding="utf-8")
    python = tmp_path / "backend" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(str(snapshot))}\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    runtime_paths = resolved_runtime_paths(tmp_path)
    report = _resolve_profile(
        {"DS_PROFILE": "integration-voice", **MODEL_ENVIRONMENT}, tmp_path
    )
    derived = report["derivedEnvironment"]
    registry = create_service_registry(
        tmp_path,
        runtime_paths,
        ollama_model_name=derived["OLLAMA_CHAT_MODEL"],
        whisper_model_name=derived["WHISPER_MODEL"],
    )

    result = require_service_operations(registry, "backend").verify(
        resolved_profile()["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    whisper_check = next(
        check for check in result.checks if check.name == "whisper-model-large-v3"
    )
    assert whisper_check.classification == "ready"
    assert "large-v3" in whisper_check.message


@pytest.mark.parametrize("availability_path", ["reused", "started"])
@pytest.mark.parametrize("model_is_available", [True, False])
def test_should_only_pull_the_profile_model_when_managed_ollama_is_missing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    availability_path: str,
    model_is_available: bool,
) -> None:
    from adapters import ollama
    import environment_runtime
    from environment_runtime import EnvironmentRun
    from environment_timing import EnvironmentTiming
    from http_readiness import ReadinessResult
    from run_report import create_initial_report
    from service_registry import create_service_registry

    runtime_paths = resolved_runtime_paths(tmp_path)
    report = _resolve_profile(
        {"DS_PROFILE": "integration-voice", **MODEL_ENVIRONMENT}, tmp_path
    )
    derived = report["derivedEnvironment"]
    report = {
        **report,
        "dependencies": {
            name: dependency
            if name == "ollama"
            else {"mode": "disabled", "source": None}
            for name, dependency in report["dependencies"].items()
        },
    }
    runner = RecordingRunner()
    registry = create_service_registry(
        tmp_path,
        runtime_paths,
        runner,
        ollama_model_name=derived["OLLAMA_CHAT_MODEL"],
        whisper_model_name=derived["WHISPER_MODEL"],
    )
    monkeypatch.setattr(
        ollama.OllamaAdapter,
        "probe",
        lambda self, dependency, timeout_seconds: ReadinessResult(
            dependency["readinessUrl"], 1, 0.001, "ready"
        ),
    )
    monkeypatch.setattr(
        environment_runtime,
        "wait_for_http",
        lambda url, **_options: ReadinessResult(url, 1, 0.001, "ready"),
    )
    monkeypatch.setattr(
        ollama.shutil,
        "which",
        lambda _command: "/usr/bin/ollama",
    )

    current_report = create_initial_report(
        run_id="model-settings-flow",
        started_at="2026-08-02T00:00:00+00:00",
        resolved_profile_path=tmp_path / "resolved-profile.json",
        effective_profile=report,
        orchestrator_identity=orchestrator_identity(),
        runtime=report["runtime"],
    )
    store = MagicMock()

    def update(transform):
        nonlocal current_report
        current_report = transform(current_report)
        return current_report

    store.update.side_effect = update
    available_model: dict[str, object] = {
        "models": [{"name": MODEL_ENVIRONMENT["OLLAMA_CHAT_MODEL"]}]
    }

    def fetch_models(_url: str) -> dict[str, object]:
        if model_is_available or runner.calls:
            return available_model
        return {"models": [{"name": "other:latest"}]}

    monkeypatch.setattr(ollama, "_fetch_json", fetch_models)
    run = EnvironmentRun(
        profile=dict(report),
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=current_report,
        ready_gate_url="http://127.0.0.1:0/ready",
        was_interrupted=lambda: False,
        registry=registry,
        timing=EnvironmentTiming(),
    )
    if availability_path == "reused":
        run.verify()
        run.pre_probe()
    else:
        run.wait_until_ready()

    expected_calls = [] if model_is_available else [
        ("ollama", "pull", MODEL_ENVIRONMENT["OLLAMA_CHAT_MODEL"])
    ]
    assert runner.calls == expected_calls


def test_should_prepare_the_same_profile_whisper_model(tmp_path: Path) -> None:
    from adapters.base import OperationContext
    from service_registry import create_service_registry, require_service_operations

    runtime_paths = resolved_runtime_paths(tmp_path)
    report = _resolve_profile(
        {"DS_PROFILE": "integration-voice", **MODEL_ENVIRONMENT}, tmp_path
    )
    derived = report["derivedEnvironment"]
    runner = RecordingRunner()
    registry = create_service_registry(
        tmp_path,
        runtime_paths,
        runner,
        ollama_model_name=derived["OLLAMA_CHAT_MODEL"],
        whisper_model_name=derived["WHISPER_MODEL"],
    )

    require_service_operations(registry, "backend").prepare(
        report["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert len(runner.calls) == 2
    download_command = runner.calls[1]
    assert download_command[:2] == (
        str(tmp_path / "backend" / ".venv" / "bin" / "python"),
        "-c",
    )
    assert download_command[3] == "large-v3"
    assert download_command[4] == str(
        runtime_paths.whisper_cache_path
    )

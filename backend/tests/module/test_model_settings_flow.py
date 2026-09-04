import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.model_settings import MODEL_ENVIRONMENT_KEYS
from tests.environment_test_support import (
    RecordingRunner,
    orchestrator_identity,
    resolved_runtime_paths,
)

MODEL_ENVIRONMENT = {
    "WHISPER_MODEL": "large-v3",
    "INFERENCE_TARGET_CHAT": "ollama/profile-chat:12b",
    "INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS": "10752",
    "INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS": "1536",
    "INFERENCE_TARGET_PRIVACY": "ollama/profile-classifier:4b",
    "INFERENCE_TARGET_PRIVACY_MAX_INPUT_TOKENS": "11776",
    "INFERENCE_TARGET_PRIVACY_MAX_OUTPUT_TOKENS": "512",
    "INFERENCE_TARGET_MEMORY_EXTRACTION": "ollama/profile-extractor:4b",
    "INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_INPUT_TOKENS": "11776",
    "INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS": "512",
    "INFERENCE_TARGET_MEMORY_CONSOLIDATION": "ollama/profile-extractor:4b",
    "INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_INPUT_TOKENS": "11776",
    "INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_OUTPUT_TOKENS": "512",
    "INFERENCE_TARGET_EMBEDDING": "ollama/nomic-embed-text:latest",
    "INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS": "12288",
    "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": "6",
    "CONVERSATION_HISTORY_TOKEN_LIMIT": "3000",
    "USER_INPUT_TOKEN_LIMIT": "1000",
    "LLM_CONTEXT_TOKEN_LIMIT": "16384",
}


def _resolve_profile(
    environment: dict[str, str], root_dir: Path = Path("/test/repository")
):
    from profile_resolution import resolve_profile

    return resolve_profile(environment, None, resolved_runtime_paths(root_dir))


def test_should_resolve_profile_overrides_into_canonical_child_environment() -> None:
    report = _resolve_profile({"DS_PROFILE": "integration-voice", **MODEL_ENVIRONMENT})

    derived = report["derivedEnvironment"]
    assert {key: derived[key] for key in MODEL_ENVIRONMENT} == MODEL_ENVIRONMENT


def test_should_not_synthesize_infrastructure_dependent_targets() -> None:
    report = _resolve_profile({"DS_PROFILE": "integration-text"})

    derived = report["derivedEnvironment"]
    assert derived["WHISPER_MODEL"] == "medium"
    assert not any(key.startswith("INFERENCE_TARGET_") for key in derived)


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
        json.dumps(_resolve_profile({"DS_PROFILE": "test-mocked"})),
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
                "OLLAMA_CHAT_MODEL": "legacy:latest",
            },
        )

    message = str(exc_info.value)
    assert "legacy inference setting" in message


def test_should_route_profile_ollama_model_to_readiness_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from adapters import ollama
    from service_registry import create_service_registry, require_service_operations

    monkeypatch.setattr(
        ollama,
        "_fetch_json",
        lambda _url: {"models": [{"name": "profile-chat:12b"}]},
    )
    runtime_paths = resolved_runtime_paths(tmp_path)
    report = _resolve_profile(
        {"DS_PROFILE": "integration-voice", **MODEL_ENVIRONMENT}, tmp_path
    )
    derived = report["derivedEnvironment"]
    registry = create_service_registry(
        tmp_path,
        runtime_paths,
        ollama_model_name=derived["INFERENCE_TARGET_CHAT"].split("/", 1)[1],
        whisper_model_name=derived["WHISPER_MODEL"],
    )

    result = require_service_operations(registry, "ollama").validate_readiness(
        report["dependencies"]["ollama"]
    )

    assert result.classification == "ready"
    assert result.message is None


def test_should_not_route_external_whisper_model_to_backend_cache_check(
    tmp_path: Path,
) -> None:
    from adapters.base import OperationContext
    from service_registry import create_service_registry, require_service_operations

    runtime_paths = resolved_runtime_paths(tmp_path)
    report = _resolve_profile(
        {"DS_PROFILE": "integration-voice", **MODEL_ENVIRONMENT}, tmp_path
    )
    derived = report["derivedEnvironment"]
    registry = create_service_registry(
        tmp_path,
        runtime_paths,
        ollama_model_name=derived["INFERENCE_TARGET_CHAT"].split("/", 1)[1],
        whisper_model_name=derived["WHISPER_MODEL"],
    )

    result = require_service_operations(registry, "backend").verify(
        report["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert all(not check.name.startswith("whisper-model-") for check in result.checks)


@pytest.mark.parametrize("model_is_available", [True, False])
def test_should_validate_but_not_prepare_the_profile_model_for_external_ollama(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model_is_available: bool,
) -> None:
    import environment_runtime
    from adapters import ollama
    from environment_runtime import EnvironmentRun
    from environment_timing import EnvironmentTiming
    from http_readiness import ReadinessResult
    from run_report import create_initial_report
    from service_registry import create_service_registry

    runtime_paths = resolved_runtime_paths(tmp_path)
    report = _resolve_profile(
        {"DS_PROFILE": "integration-voice", **MODEL_ENVIRONMENT}, tmp_path
    )
    report = {
        **report,
        "dependencies": {
            name: dependency
            if name == "ollama"
            else {"mode": "disabled", "source": None}
            for name, dependency in report["dependencies"].items()
            if name != "livekit"
        },
    }
    derived = report["derivedEnvironment"]
    runner = RecordingRunner()
    registry = create_service_registry(
        tmp_path,
        runtime_paths,
        runner,
        ollama_model_name=derived["INFERENCE_TARGET_CHAT"].split("/", 1)[1],
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
    models = (
        [{"name": "profile-chat:12b"}]
        if model_is_available
        else [{"name": "other:latest"}]
    )
    monkeypatch.setattr(ollama, "_fetch_json", lambda _url: {"models": models})
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
    run = EnvironmentRun(
        profile=dict(report),
        profile_path=tmp_path / "resolved-profile.json",
        store=store,
        report=current_report,
        ready_gate={"baseUrl": "http://127.0.0.1:0", "host": "127.0.0.1", "port": 0},
        was_interrupted=lambda: False,
        registry=registry,
        timing=EnvironmentTiming(),
    )

    decisions = run.pre_probe()

    assert decisions == {"ollama": "external"}
    assert current_report["services"]["ollama"]["state"] == "external"
    assert current_report["services"]["ollama"]["owned"] is False

    if model_is_available:
        run.wait_until_ready()
    else:
        with pytest.raises(RuntimeError):
            run.wait_until_ready()

    assert runner.calls == []


def test_should_not_prepare_external_whisper_model_in_backend(tmp_path: Path) -> None:
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
        ollama_model_name=derived["INFERENCE_TARGET_CHAT"].split("/", 1)[1],
        whisper_model_name=derived["WHISPER_MODEL"],
    )

    require_service_operations(registry, "backend").prepare(
        report["dependencies"]["backend"],
        OperationContext(whisper_enabled=True, chroma_enabled=False),
    )

    assert runner.calls == []

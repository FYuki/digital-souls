import os
import subprocess
import sys
from pathlib import Path

import pytest
from app.model_settings import MODEL_ENVIRONMENT_KEYS
from tests.environment_entrypoint_test_support import copy_environment_runtime


ROOT_DIR = Path(__file__).parent.parent.parent.parent


def _report_path(tmp_path: Path) -> Path:
    return tmp_path / "runtime-data" / "runtime" / "resolved.json"


def _copy_environments(tmp_path: Path) -> Path:
    return copy_environment_runtime(tmp_path)


def _run(environments_dir: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    blocked = {
        "DS_PROFILE",
        "DS_PROFILE_REPORT",
        "VOICE_CHAT_E2E_BACKEND",
        "CHAT_E2E_BACKEND",
        "CHAT_E2E_BACKEND_ORIGIN",
        "VOICE_CHAT_E2E_BACKEND_REPORT",
    }
    env = {key: value for key, value in os.environ.items() if key not in blocked}
    env.update(
        DS_ENVIRONMENT_ID="test",
        DS_DATA_DIR=str(environments_dir.parent / "runtime-data"),
    )
    return subprocess.run(
        [sys.executable, str(environments_dir / "profile.py"), *arguments],
        env=env,
        capture_output=True,
        text=True,
    )


def _write_mocked_report(environments_dir: Path, report_path: Path) -> None:
    result = _run(
        environments_dir,
        "resolve",
        "--report",
        str(report_path),
        "--default-profile",
        "test-mocked",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("dependencies.backend.mode", "mock\n"),
        ("reportSchemaVersion", "1\n"),
        ("derivedEnvironment.RAG_ENABLED", "false\n"),
        ("dependencies.chroma.source", "null\n"),
    ],
)
def test_should_get_scalar_report_value(path: str, expected: str, tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = _report_path(tmp_path)
    _write_mocked_report(environments_dir, report_path)

    result = _run(
        environments_dir,
        "get",
        "--report",
        str(report_path),
        "--path",
        path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


def test_should_list_backend_owned_model_environment_keys(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)

    result = _run(environments_dir, "model-environment-keys")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == list(MODEL_ENVIRONMENT_KEYS)


def test_should_list_inference_environment_keys(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)

    result = _run(environments_dir, "inference-environment-keys")

    assert result.returncode == 0, result.stderr
    assert "INFERENCE_TARGET_CHAT" in result.stdout.splitlines()
    assert "INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS" in result.stdout.splitlines()


def test_should_list_only_configured_inference_environment_keys(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = _report_path(tmp_path)
    resolved = _run(
        environments_dir,
        "resolve",
        "--report",
        str(report_path),
        "--default-profile",
        "dev",
    )
    assert resolved.returncode == 0, resolved.stderr

    result = _run(
        environments_dir,
        "configured-inference-environment-keys",
        "--report",
        str(report_path),
    )

    assert result.returncode == 0, result.stderr
    keys = result.stdout.splitlines()
    assert "INFERENCE_TARGET_CHAT" in keys
    assert "INFERENCE_TARGET_CHAT_OPTIONS_JSON" not in keys


@pytest.mark.parametrize("path", ["dependencies.missing.mode", "dependencies"])
def test_should_reject_missing_or_non_scalar_get_path(path: str, tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = _report_path(tmp_path)
    _write_mocked_report(environments_dir, report_path)

    result = _run(
        environments_dir,
        "get",
        "--report",
        str(report_path),
        "--path",
        path,
    )

    assert result.returncode != 0
    assert path in result.stderr


def test_should_report_directory_input_without_a_traceback(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)

    result = _run(
        environments_dir,
        "get",
        "--report",
        str(tmp_path),
        "--path",
        "dependencies.backend.mode",
    )

    assert result.returncode != 0
    assert "report" in result.stderr
    assert str(tmp_path) in result.stderr
    assert "Traceback" not in result.stderr

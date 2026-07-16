import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast

import pytest


ROOT_DIR = Path(__file__).parent.parent.parent
ENVIRONMENTS_DIR = ROOT_DIR / "environments"
PROFILE_NAMES = ["dev", "test-mocked", "integration-text", "integration-voice"]
JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


def _copy_environments(tmp_path: Path) -> Path:
    target = tmp_path / "environments"
    shutil.copytree(ENVIRONMENTS_DIR, target)
    return target


def _run_cli(
    environments_dir: Path,
    *arguments: str,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DS_PROFILE",
            "DS_PROFILE_REPORT",
            "VOICE_CHAT_E2E_BACKEND",
            "CHAT_E2E_BACKEND",
            "CHAT_E2E_BACKEND_ORIGIN",
            "VOICE_CHAT_E2E_BACKEND_REPORT",
        }
    }
    if env_overrides is not None:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(environments_dir / "profile.py"), *arguments],
        env=env,
        capture_output=True,
        text=True,
    )


def _load_report(path: Path) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("profile_name", PROFILE_NAMES)
def test_should_validate_each_initial_profile(profile_name: str, tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)

    result = _run_cli(environments_dir, "validate", "--profile", profile_name)

    assert result.returncode == 0, result.stderr


def test_should_reject_unknown_fields_with_identifiable_path(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    profile_path = environments_dir / "profiles" / "dev.json"
    profile = _load_report(profile_path)
    profile["unexpected"] = True
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = _run_cli(environments_dir, "validate", "--profile", "dev")

    assert result.returncode != 0
    assert "dev" in result.stderr
    assert "unexpected" in result.stderr


def test_should_reject_unknown_dependency_fields_with_identifiable_path(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    profile_path = environments_dir / "profiles" / "dev.json"
    profile = _load_report(profile_path)
    profile["dependencies"]["backend"]["unexpected"] = True
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = _run_cli(environments_dir, "validate", "--profile", "dev")

    assert result.returncode != 0
    assert "dependencies.backend.unexpected" in result.stderr


@pytest.mark.parametrize(
    ("mutation", "expected_path"),
    [
        (lambda profile: profile.pop("dependencies"), "dependencies"),
        (
            lambda profile: profile["dependencies"].pop("chroma"),
            "dependencies.chroma",
        ),
        (lambda profile: profile.update(schemaVersion=2), "schemaVersion"),
        (
            lambda profile: profile["dependencies"]["backend"].update(mode="auto"),
            "dependencies.backend.mode",
        ),
        (
            lambda profile: profile["dependencies"]["backend"].update(source="worker"),
            "dependencies.backend.source",
        ),
        (
            lambda profile: profile["dependencies"]["backend"].pop("source"),
            "dependencies.backend.source",
        ),
        (
            lambda profile: profile["dependencies"]["chroma"].update(
                mode="disabled", source="in_process"
            ),
            "dependencies.chroma.source",
        ),
        (
            lambda profile: profile["dependencies"]["backend"].pop("baseUrl"),
            "dependencies.backend.baseUrl",
        ),
        (
            lambda profile: profile["dependencies"]["backend"].pop("readinessPath"),
            "dependencies.backend.readinessPath",
        ),
        (
            lambda profile: profile["dependencies"]["backend"].update(
                baseUrl="file:///tmp/backend"
            ),
            "dependencies.backend.baseUrl",
        ),
        (
            lambda profile: profile["dependencies"]["whisper"].update(source="managed"),
            "dependencies.whisper.source",
        ),
        (
            lambda profile: profile["dependencies"]["chroma"].update(
                mode="real", source="managed", baseUrl="http://localhost:8001"
            ),
            "dependencies.chroma.source",
        ),
        (
            lambda profile: profile["dependencies"]["ollama"].update(
                mode="mock", source="browser"
            ),
            "dependencies.ollama.mode",
        ),
        (
            lambda profile: profile["dependencies"]["voicevox"].update(
                baseUrl="http://voicevox.example:50021"
            ),
            "dependencies.voicevox.baseUrl",
        ),
        (
            lambda profile: profile["dependencies"]["voicevox"].update(
                readinessPath="/health"
            ),
            "dependencies.voicevox.readinessPath",
        ),
    ],
)
def test_should_reject_static_profile_violation(
    mutation,
    expected_path: str,
    tmp_path: Path,
):
    environments_dir = _copy_environments(tmp_path)
    profile_path = environments_dir / "profiles" / "dev.json"
    profile = _load_report(profile_path)
    mutation(profile)
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = _run_cli(environments_dir, "validate", "--profile", "dev")

    assert result.returncode != 0
    assert expected_path in result.stderr


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("mode", []),
        ("mode", {}),
        ("source", []),
        ("source", {}),
    ],
)
def test_should_reject_non_scalar_dependency_fields_without_traceback(
    field: str,
    invalid_value: object,
    tmp_path: Path,
):
    environments_dir = _copy_environments(tmp_path)
    profile_path = environments_dir / "profiles" / "dev.json"
    profile = _load_report(profile_path)
    profile["dependencies"]["backend"][field] = invalid_value
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = _run_cli(environments_dir, "validate", "--profile", "dev")

    assert result.returncode != 0
    assert f"dependencies.backend.{field}" in result.stderr
    assert "Traceback" not in result.stderr


def test_should_reject_boolean_schema_version_with_profile_and_field(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    profile_path = environments_dir / "profiles" / "dev.json"
    profile = _load_report(profile_path)
    profile["schemaVersion"] = True
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = _run_cli(environments_dir, "validate", "--profile", "dev")

    assert result.returncode != 0
    assert "dev" in result.stderr
    assert "schemaVersion" in result.stderr


@pytest.mark.parametrize("backend_mode", ["mock", "disabled"])
def test_should_reject_enabled_downstream_for_non_real_backend(
    backend_mode: str,
    tmp_path: Path,
):
    environments_dir = _copy_environments(tmp_path)
    profile_path = environments_dir / "profiles" / "dev.json"
    profile = _load_report(profile_path)
    profile["dependencies"]["backend"] = {
        "mode": backend_mode,
        "source": "browser" if backend_mode == "mock" else None,
    }
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = _run_cli(environments_dir, "validate", "--profile", "dev")

    assert result.returncode != 0
    assert "dependencies.ollama" in result.stderr


def test_should_reject_profile_name_that_differs_from_filename(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    profile_path = environments_dir / "profiles" / "dev.json"
    profile = _load_report(profile_path)
    profile["name"] = "renamed"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    result = _run_cli(environments_dir, "validate", "--profile", "dev")

    assert result.returncode != 0
    assert "name" in result.stderr
    assert "dev" in result.stderr


def test_should_reject_profile_path_traversal(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)

    result = _run_cli(environments_dir, "validate", "--profile", "../outside")

    assert result.returncode != 0
    assert "profile" in result.stderr.lower()


def test_should_report_invalid_json_with_profile_identity(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    (environments_dir / "profiles" / "dev.json").write_text("{", encoding="utf-8")

    result = _run_cli(environments_dir, "validate", "--profile", "dev")

    assert result.returncode != 0
    assert "dev" in result.stderr
    assert "json" in result.stderr.lower()

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).parent.parent.parent
ENVIRONMENTS_DIR = ROOT_DIR / "environments"


def _copy_environments(tmp_path: Path) -> Path:
    target = tmp_path / "environments"
    shutil.copytree(ENVIRONMENTS_DIR, target)
    return target


def _resolve(
    environments_dir: Path,
    report_path: Path,
    *,
    env_overrides: dict[str, str] | None = None,
    default_profile: str | None = None,
) -> subprocess.CompletedProcess[str]:
    blocked = {
        "DS_PROFILE",
        "DS_PROFILE_REPORT",
        "VOICE_CHAT_E2E_BACKEND",
        "CHAT_E2E_BACKEND",
        "CHAT_E2E_BACKEND_ORIGIN",
        "VOICE_CHAT_E2E_BACKEND_REPORT",
    }
    env = {key: value for key, value in os.environ.items() if key not in blocked}
    if env_overrides is not None:
        env.update(env_overrides)
    arguments = ["resolve", "--report", str(report_path)]
    if default_profile is not None:
        arguments.extend(["--default-profile", default_profile])
    return subprocess.run(
        [sys.executable, str(environments_dir / "profile.py"), *arguments],
        env=env,
        capture_output=True,
        text=True,
    )


def _read_report(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_should_select_ds_profile_before_matching_legacy_selector(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(
        environments_dir,
        report_path,
        env_overrides={"DS_PROFILE": "test-mocked", "CHAT_E2E_BACKEND": "mock"},
        default_profile="dev",
    )

    assert result.returncode == 0, result.stderr
    report = _read_report(report_path)
    assert report["requestedProfile"] == "test-mocked"
    assert report["effectiveProfile"] == "test-mocked"
    assert report["selectionSource"] == "DS_PROFILE"
    assert "CHAT_E2E_BACKEND" in report["compatibility"]["usedEnvironmentVariables"]
    assert report["compatibility"]["warnings"]


def test_should_allow_different_profile_names_with_same_effective_dependencies(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(
        environments_dir,
        report_path,
        env_overrides={"DS_PROFILE": "dev", "VOICE_CHAT_E2E_BACKEND": "real"},
    )

    assert result.returncode == 0, result.stderr
    report = _read_report(report_path)
    assert report["effectiveProfile"] == "dev"
    assert report["compatibility"]["warnings"]


def test_should_select_explicit_default_when_no_environment_selector_exists(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(environments_dir, report_path, default_profile="integration-text")

    assert result.returncode == 0, result.stderr
    report = _read_report(report_path)
    assert report["effectiveProfile"] == "integration-text"
    assert report["selectionSource"] == "default-profile"


def test_should_fail_without_any_profile_selection_source(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(environments_dir, report_path)

    assert result.returncode != 0
    assert "profile" in result.stderr.lower()
    assert not report_path.exists()


def test_should_reject_empty_ds_profile_instead_of_using_default(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(
        environments_dir,
        report_path,
        env_overrides={"DS_PROFILE": ""},
        default_profile="dev",
    )

    assert result.returncode != 0
    assert "DS_PROFILE" in result.stderr
    assert not report_path.exists()


@pytest.mark.parametrize(
    ("legacy_name", "legacy_value", "effective_profile"),
    [
        ("VOICE_CHAT_E2E_BACKEND", "mock", "test-mocked"),
        ("VOICE_CHAT_E2E_BACKEND", "real", "integration-voice"),
        ("CHAT_E2E_BACKEND", "mock", "test-mocked"),
        ("CHAT_E2E_BACKEND", "real", "integration-text"),
    ],
)
def test_should_convert_each_legacy_selector(
    legacy_name: str,
    legacy_value: str,
    effective_profile: str,
    tmp_path: Path,
):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(
        environments_dir,
        report_path,
        env_overrides={legacy_name: legacy_value},
    )

    assert result.returncode == 0, result.stderr
    report = _read_report(report_path)
    assert report["effectiveProfile"] == effective_profile
    assert report["selectionSource"] == "legacy-environment"
    assert legacy_name in report["compatibility"]["usedEnvironmentVariables"]
    assert report["compatibility"]["warnings"]


def test_should_reject_conflicting_new_and_legacy_selectors(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(
        environments_dir,
        report_path,
        env_overrides={"DS_PROFILE": "integration-voice", "CHAT_E2E_BACKEND": "mock"},
    )

    assert result.returncode != 0
    assert "DS_PROFILE" in result.stderr
    assert "CHAT_E2E_BACKEND" in result.stderr
    assert not report_path.exists()


def test_should_resolve_compatible_real_legacy_selectors_to_voice_profile(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(
        environments_dir,
        report_path,
        env_overrides={
            "VOICE_CHAT_E2E_BACKEND": "real",
            "CHAT_E2E_BACKEND": "real",
        },
    )

    assert result.returncode == 0, result.stderr
    assert _read_report(report_path)["effectiveProfile"] == "integration-voice"


def test_should_reject_legacy_selectors_that_cannot_form_one_profile(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(
        environments_dir,
        report_path,
        env_overrides={
            "VOICE_CHAT_E2E_BACKEND": "mock",
            "CHAT_E2E_BACKEND": "real",
        },
    )

    assert result.returncode != 0
    assert "VOICE_CHAT_E2E_BACKEND" in result.stderr
    assert "CHAT_E2E_BACKEND" in result.stderr


def test_should_reject_unknown_legacy_selector_value(tmp_path: Path):
    environments_dir = _copy_environments(tmp_path)
    report_path = tmp_path / "resolved.json"

    result = _resolve(
        environments_dir,
        report_path,
        env_overrides={"VOICE_CHAT_E2E_BACKEND": "auto"},
    )

    assert result.returncode != 0
    assert "VOICE_CHAT_E2E_BACKEND" in result.stderr
    assert "auto" in result.stderr

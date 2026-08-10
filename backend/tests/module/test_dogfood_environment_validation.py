from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.dogfood_infrastructure_test_support import (
    DOGFOOD_SCRIPTS_DIR,
    write_dogfood_env,
)


def _run_loader(env_path: Path, sentinel_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; dogfood_load_environment && touch "$2"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
            str(sentinel_path),
        ],
        env={**os.environ, "DOGFOOD_ENV_FILE": str(env_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )


def _replace_setting(env_path: Path, key: str, value: str) -> None:
    source = env_path.read_text(encoding="utf-8")
    lines = source.splitlines()
    updated = [f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines]
    env_path.write_text("\n".join(updated), encoding="utf-8")


def test_should_continue_after_loading_valid_dogfood_environment(tmp_path: Path) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    sentinel_path = tmp_path / "valid.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode == 0, result.stderr
    assert sentinel_path.is_file()


@pytest.mark.parametrize(
    "invalid_line",
    (
        "UNKNOWN_DOGFOOD_KEY=value",
        "DS_ENVIRONMENT_ID=dogfood",
    ),
    ids=("unknown-key", "duplicate-key"),
)
def test_should_reject_invalid_env_line_before_following_operation(
    tmp_path: Path,
    invalid_line: str,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    with env_path.open("a", encoding="utf-8") as env_file:
        env_file.write(f"\n{invalid_line}\n")
    sentinel_path = tmp_path / "invalid-line.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


def test_should_reject_empty_setting_before_following_operation(tmp_path: Path) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    _replace_setting(env_path, "DOGFOOD_VOICEVOX_CONTAINER", "")
    sentinel_path = tmp_path / "empty.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


def test_should_reject_missing_required_setting_before_following_operation(
    tmp_path: Path,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    source = env_path.read_text(encoding="utf-8")
    env_path.write_text(
        "\n".join(
            line
            for line in source.splitlines()
            if not line.startswith("DOGFOOD_VOICEVOX_CONTAINER=")
        ),
        encoding="utf-8",
    )
    sentinel_path = tmp_path / "missing.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


@pytest.mark.parametrize(
    "key",
    (
        "DOGFOOD_BACKUP_DIR",
        "DOGFOOD_BACKUP_RETENTION_COUNT",
        "DOGFOOD_BACKUP_AUTHENTICATION_KEY",
    ),
)
def test_should_reject_missing_required_backup_setting_before_following_operation(
    tmp_path: Path,
    key: str,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    source = env_path.read_text(encoding="utf-8")
    env_path.write_text(
        "\n".join(
            line for line in source.splitlines() if not line.startswith(f"{key}=")
        ),
        encoding="utf-8",
    )
    sentinel_path = tmp_path / "missing-backup-setting.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


@pytest.mark.parametrize(
    "key",
    (
        "DOGFOOD_BACKUP_DIR",
        "DOGFOOD_BACKUP_RETENTION_COUNT",
        "DOGFOOD_BACKUP_AUTHENTICATION_KEY",
    ),
)
def test_should_reject_empty_backup_setting_before_following_operation(
    tmp_path: Path,
    key: str,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    _replace_setting(env_path, key, "")
    sentinel_path = tmp_path / "empty-backup-setting.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


@pytest.mark.parametrize(
    ("key", "invalid_value"),
    (
        ("DOGFOOD_WSL_DISTRO", "Ubuntu dogfood"),
        ("DOGFOOD_SERVICE_USER", "Digital-Souls"),
        ("DOGFOOD_SERVICE_GROUP", "digital souls"),
        ("DOGFOOD_CLONE_DIR", "relative/clone"),
        ("DOGFOOD_REPOSITORY_URL", "http://example.invalid/repository.git"),
        ("DOGFOOD_REPOSITORY_REVISION", "0123456789abcdef"),
        ("DOGFOOD_BACKUP_RETENTION_COUNT", "0"),
        ("DOGFOOD_BACKUP_RETENTION_COUNT", "seven"),
        ("DOGFOOD_BACKUP_AUTHENTICATION_KEY", "0123456789abcdef"),
    ),
    ids=(
        "wsl-distribution",
        "service-user",
        "service-group",
        "absolute-path",
        "repository-url",
        "repository-revision",
        "backup-retention-count",
        "backup-retention-nonnumeric",
        "backup-authentication-key",
    ),
)
def test_should_reject_invalid_setting_format_before_following_operation(
    tmp_path: Path,
    key: str,
    invalid_value: str,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    _replace_setting(env_path, key, invalid_value)
    sentinel_path = tmp_path / "invalid-format.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()


@pytest.mark.parametrize(
    ("left_key", "right_key"),
    (
        ("DOGFOOD_CLONE_DIR", "DOGFOOD_LOG_DIR"),
        ("DOGFOOD_CONFIG_DIR", "DS_DATA_DIR"),
        ("DS_DATA_DIR", "DOGFOOD_STATE_DIR"),
        ("DS_DATA_DIR", "DOGFOOD_BACKUP_DIR"),
    ),
    ids=(
        "first-and-last",
        "adjacent-middle",
        "middle-and-later",
        "data-and-backup",
    ),
)
def test_should_reject_overlapping_paths_before_following_operation(
    tmp_path: Path,
    left_key: str,
    right_key: str,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    shared_path = tmp_path / "shared"
    _replace_setting(env_path, left_key, str(shared_path))
    _replace_setting(env_path, right_key, str(shared_path / "child"))
    sentinel_path = tmp_path / "overlap.sentinel"

    result = _run_loader(env_path, sentinel_path)

    assert result.returncode != 0
    assert not sentinel_path.exists()

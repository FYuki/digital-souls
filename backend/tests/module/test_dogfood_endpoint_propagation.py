from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from tests.dogfood_infrastructure_test_support import write_dogfood_env
from tests.environment_entrypoint_test_support import (
    ROOT_DIR,
    copy_environment_runtime,
    write_executable,
)


DOGFOOD_INFRA_DIR = ROOT_DIR / "infra" / "dogfood"
DOGFOOD_SCRIPTS_DIR = ROOT_DIR / "scripts" / "dogfood"
OLLAMA_ENDPOINT = ("localhost", 11434)
VOICEVOX_ENDPOINT = ("127.0.0.1", 50021)


def _copy_runtime(tmp_path: Path) -> tuple[Path, Path, Path]:
    assert DOGFOOD_SCRIPTS_DIR.is_dir(), "dogfood operation scripts are required"
    assert DOGFOOD_INFRA_DIR.is_dir(), "dogfood infrastructure assets are required"
    runtime_root = tmp_path / "runtime"
    copy_environment_runtime(runtime_root)
    shutil.copytree(DOGFOOD_SCRIPTS_DIR, runtime_root / "scripts" / "dogfood")
    shutil.copytree(DOGFOOD_INFRA_DIR, runtime_root / "infra" / "dogfood")
    env_path, _ = write_dogfood_env(tmp_path)
    env_source = env_path.read_text(encoding="utf-8").replace(
        f"DOGFOOD_CLONE_DIR={tmp_path / 'clone'}",
        f"DOGFOOD_CLONE_DIR={runtime_root}",
    )
    env_path.write_text(env_source, encoding="utf-8")
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    render_result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; dogfood_load_environment; "$2" "$3" "$4"',
            "bash",
            str(runtime_root / "scripts" / "dogfood" / "load-environment.sh"),
            str(runtime_root / "scripts" / "dogfood" / "render-assets.sh"),
            str(runtime_root / "infra" / "dogfood" / "templates"),
            str(generated_dir),
        ],
        env={**os.environ, "DOGFOOD_ENV_FILE": str(env_path)},
        capture_output=True,
        text=True,
    )
    assert render_result.returncode == 0, render_result.stderr
    return runtime_root, env_path, generated_dir


def _render_profile(tmp_path: Path, dependency_name: str, field: str, value: str):
    runtime_root = tmp_path / "runtime"
    environments_dir = copy_environment_runtime(runtime_root)
    shutil.copytree(DOGFOOD_SCRIPTS_DIR, runtime_root / "scripts" / "dogfood")
    profile_path = environments_dir / "profiles" / "dogfood.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    candidate = deepcopy(profile)
    candidate["dependencies"][dependency_name][field] = value
    profile_path.write_text(json.dumps(candidate), encoding="utf-8")
    return subprocess.run(
        [str(runtime_root / "scripts" / "dogfood" / "resolve-inference-endpoints.py")],
        capture_output=True,
        text=True,
    )


def _install_recording_commands(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "commands.log"
    for name in ("systemctl", "ss", "ps", "free", "nvidia-smi"):
        write_executable(
            bin_dir / name,
            f'printf "%s\\t%s\\n" "{name}" "$*" >> "{command_log}"\n',
        )
    write_executable(
        bin_dir / "docker",
        f'printf "docker\\t%s\\t%s\\t%s\\n" "$*" '
        f'"${{VOICEVOX_HOST-}}" "${{VOICEVOX_PORT-}}" >> "{command_log}"\n',
    )
    return bin_dir, command_log


def _voicevox_runner(runtime_root: Path, generated_dir: Path) -> Path:
    unit_paths = tuple(generated_dir.glob("*voicevox.service"))
    assert len(unit_paths) == 1
    unit = configparser.ConfigParser(interpolation=None, strict=True)
    unit.optionxform = str
    unit.read(unit_paths[0], encoding="utf-8")
    runner_match = re.search(r"[^\s]+\.sh", unit["Service"]["ExecStart"])
    assert runner_match is not None, "VOICEVOX unit must delegate to a runner"
    return runtime_root / "scripts" / "dogfood" / Path(runner_match.group()).name


def test_should_propagate_profile_endpoints_to_compose_and_status(
    tmp_path: Path,
) -> None:
    runtime_root, env_path, generated_dir = _copy_runtime(tmp_path)
    bin_dir, command_log = _install_recording_commands(tmp_path)
    execution_environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DOGFOOD_ENV_FILE": str(env_path),
        "WSL_DISTRO_NAME": "Ubuntu-dogfood",
    }

    status_result = subprocess.run(
        [str(runtime_root / "scripts" / "dogfood" / "status.sh")],
        env=execution_environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    voicevox_result = subprocess.run(
        [str(_voicevox_runner(runtime_root, generated_dir))],
        env=execution_environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert status_result.returncode == 0, (status_result.stdout, status_result.stderr)
    assert voicevox_result.returncode == 0, (
        voicevox_result.stdout,
        voicevox_result.stderr,
    )
    calls = command_log.read_text(encoding="utf-8")
    assert str(OLLAMA_ENDPOINT[1]) in calls
    assert str(VOICEVOX_ENDPOINT[1]) in calls
    assert "docker\t" in calls
    assert f"\t{VOICEVOX_ENDPOINT[0]}\t{VOICEVOX_ENDPOINT[1]}" in calls


@pytest.mark.parametrize(
    ("dependency_name", "field", "value"),
    [
        ("ollama", "baseUrl", "http://127.0.0.7:11434"),
        ("ollama", "baseUrl", "http://localhost:21134"),
        ("ollama", "readinessPath", "/health"),
        ("voicevox", "baseUrl", "http://127.0.0.8:50021"),
        ("voicevox", "baseUrl", "http://127.0.0.1:20021"),
        ("voicevox", "readinessPath", "/health"),
    ],
)
def test_should_reject_dogfood_inference_endpoints_outside_the_registry(
    tmp_path: Path,
    dependency_name: str,
    field: str,
    value: str,
) -> None:
    result = _render_profile(tmp_path, dependency_name, field, value)

    assert result.returncode != 0


def test_should_resolve_default_voicevox_endpoint_into_valid_compose_config() -> None:
    resolver_result = subprocess.run(
        [str(DOGFOOD_SCRIPTS_DIR / "resolve-inference-endpoints.py")],
        capture_output=True,
        text=True,
    )
    assert resolver_result.returncode == 0, resolver_result.stderr
    resolved_environment = dict(
        line.split("=", maxsplit=1) for line in resolver_result.stdout.splitlines()
    )
    compose_result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(DOGFOOD_INFRA_DIR / "voicevox" / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        env={
            **os.environ,
            **resolved_environment,
            "DOGFOOD_VOICEVOX_IMAGE": "voicevox/voicevox_engine:test",
            "DOGFOOD_VOICEVOX_CONTAINER": "digital-souls-voicevox-test",
        },
        capture_output=True,
        text=True,
    )

    assert compose_result.returncode == 0, compose_result.stderr
    config = json.loads(compose_result.stdout)
    port = config["services"]["voicevox"]["ports"][0]
    assert port["host_ip"] == "127.0.0.1"
    assert port["published"] == "50021"
    assert port["target"] == 50021

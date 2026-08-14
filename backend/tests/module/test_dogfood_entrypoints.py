from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from tests.dogfood_infrastructure_test_support import (
    command_with_root_owned_revision,
    write_dogfood_env,
)
from tests.environment_entrypoint_test_support import ROOT_DIR, write_executable


def _run_entrypoint(
    tmp_path: Path, script_name: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    entrypoint = ROOT_DIR / "scripts" / script_name
    assert entrypoint.is_file(), f"dogfood entrypoint is required: {script_name}"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    capture_path = tmp_path / f"{script_name}.json"
    write_executable(
        bin_dir / "python3",
        f"""if [ \"${{1-}}\" = \"-\" ]; then
  exec {sys.executable} \"$@\"
fi
{sys.executable} - \"$@\" <<'PY'
import json, os, sys
json.dump({{
    'arguments': sys.argv[1:],
    'profile': os.environ.get('DS_PROFILE'),
    'identity': os.environ.get('DS_ENVIRONMENT_ID'),
    'dataRoot': os.environ.get('DS_DATA_DIR'),
    'runReport': os.environ.get('DS_ENVIRONMENT_RUN_REPORT'),
    'profileReport': os.environ.get('DS_PROFILE_REPORT'),
}}, open({str(capture_path)!r}, 'w'))
PY
""",
    )
    env_path, generated_data_root = write_dogfood_env(tmp_path)
    data_root = tmp_path / "dogfood-data"
    source = env_path.read_text(encoding="utf-8").replace(
        f"DS_DATA_DIR={generated_data_root}",
        f"DS_DATA_DIR={data_root}",
    )
    env_path.write_text(source, encoding="utf-8")
    result = subprocess.run(
        command_with_root_owned_revision(
            tmp_path / "config" / "dogfood.revision", [str(entrypoint)]
        ),
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DOGFOOD_ENV_FILE": str(env_path),
            "WSL_DISTRO_NAME": "Ubuntu-dogfood",
            "DS_PROFILE": "caller-profile-must-not-win",
            "DS_ENVIRONMENT_ID": "dev",
            "DS_DATA_DIR": str(tmp_path / "dev-data-must-not-win"),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    return result, capture


def _run_entrypoint_with_mismatched_identity(
    tmp_path: Path,
    script_name: str,
    environment_id: str,
    wsl_distro: str,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    entrypoint = ROOT_DIR / "scripts" / script_name
    assert entrypoint.is_file(), f"dogfood entrypoint is required: {script_name}"
    bin_dir = tmp_path / "reject-bin"
    bin_dir.mkdir()
    capture_path = tmp_path / f"{script_name}.rejected"
    write_executable(
        bin_dir / "python3",
        f'if [ "${{1-}}" = "-" ]; then\n'
        f'  exec {sys.executable} "$@"\n'
        "fi\n"
        f'printf "%s\\n" "$*" > "{capture_path}"\n',
    )
    env_path, _ = write_dogfood_env(tmp_path)
    if environment_id != "dogfood":
        source = env_path.read_text(encoding="utf-8").replace(
            "DS_ENVIRONMENT_ID=dogfood",
            f"DS_ENVIRONMENT_ID={environment_id}",
        )
        env_path.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [str(entrypoint)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DOGFOOD_ENV_FILE": str(env_path),
            "WSL_DISTRO_NAME": wsl_distro,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result, capture_path


def test_should_fix_dogfood_profile_identity_and_report_for_start(tmp_path: Path) -> None:
    result, capture = _run_entrypoint(tmp_path, "start-dogfood.sh")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert capture["arguments"][1] == "up"
    assert capture["profile"] == "dogfood"
    assert capture["identity"] == "dogfood"
    assert capture["dataRoot"] == str(tmp_path / "dogfood-data")
    assert cast(str, capture["runReport"]).endswith(
        "/runtime/dogfood/environment-run.json"
    )
    assert cast(str, capture["profileReport"]).endswith(
        "/runtime/dogfood/resolved-profile.json"
    )


def test_should_use_the_same_dogfood_ownership_report_for_stop(tmp_path: Path) -> None:
    result, capture = _run_entrypoint(tmp_path, "stop-dogfood.sh")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert capture["arguments"][1] == "down"
    assert capture["profile"] == "dogfood"
    assert capture["identity"] == "dogfood"
    assert capture["dataRoot"] == str(tmp_path / "dogfood-data")
    assert capture["arguments"][-2:] == ["--run-report", capture["runReport"]]


def test_should_use_the_same_dogfood_ownership_report_for_status(tmp_path: Path) -> None:
    result, capture = _run_entrypoint(tmp_path, "status-dogfood.sh")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert capture["arguments"][1] == "status"
    assert capture["profile"] == "dogfood"
    assert capture["identity"] == "dogfood"
    assert capture["dataRoot"] == str(tmp_path / "dogfood-data")
    assert capture["arguments"][-2:] == ["--run-report", capture["runReport"]]


@pytest.mark.parametrize(
    "script_name",
    ("start-dogfood.sh", "status-dogfood.sh", "stop-dogfood.sh"),
)
@pytest.mark.parametrize(
    ("environment_id", "wsl_distro"),
    (("dev", "Ubuntu-dogfood"), ("dogfood", "Ubuntu-dev")),
)
def test_should_reject_dogfood_entrypoint_before_cli_for_mismatched_identity(
    tmp_path: Path,
    script_name: str,
    environment_id: str,
    wsl_distro: str,
) -> None:
    result, capture_path = _run_entrypoint_with_mismatched_identity(
        tmp_path, script_name, environment_id, wsl_distro
    )

    assert result.returncode != 0
    assert not capture_path.exists()

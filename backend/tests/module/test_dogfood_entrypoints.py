from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

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
        f"""{sys.executable} - \"$@\" <<'PY'
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
    data_root = tmp_path / "dogfood-data"
    result = subprocess.run(
        [str(entrypoint)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DS_PROFILE": "caller-profile-must-not-win",
            "DS_ENVIRONMENT_ID": "dev",
            "DS_DATA_DIR": str(data_root),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    return result, capture


def test_should_fix_dogfood_profile_identity_and_report_for_start(tmp_path: Path) -> None:
    result, capture = _run_entrypoint(tmp_path, "start-dogfood.sh")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert capture["arguments"][1] == "up"
    assert capture["profile"] == "dogfood"
    assert capture["identity"] == "dogfood"
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
    assert capture["arguments"][-2:] == ["--run-report", capture["runReport"]]

def test_should_use_the_same_dogfood_ownership_report_for_status(tmp_path: Path) -> None:
    result, capture = _run_entrypoint(tmp_path, "status-dogfood.sh")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert capture["arguments"][1] == "status"
    assert capture["profile"] == "dogfood"
    assert capture["identity"] == "dogfood"
    assert capture["arguments"][-2:] == ["--run-report", capture["runReport"]]

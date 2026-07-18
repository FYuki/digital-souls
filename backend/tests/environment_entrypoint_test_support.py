from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).parent.parent.parent


def copy_environment_runtime(tmp_path: Path) -> Path:
    environments = tmp_path / "environments"
    shutil.copytree(ROOT_DIR / "environments", environments)
    backend_app = tmp_path / "backend" / "app"
    backend_app.mkdir(parents=True)
    shutil.copy2(ROOT_DIR / "backend" / "app" / "__init__.py", backend_app / "__init__.py")
    shutil.copy2(
        ROOT_DIR / "backend" / "app" / "model_settings.py",
        backend_app / "model_settings.py",
    )
    return environments


def write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}", encoding="utf-8")
    path.chmod(0o755)


def run_with_recording_python(
    entrypoint: Path,
    arguments: list[str],
    tmp_path: Path,
    exit_code: int,
) -> tuple[subprocess.Popen[str], str, str, list[str], int]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    arguments_path = tmp_path / "arguments.json"
    pid_path = tmp_path / "python.pid"
    write_executable(
        bin_dir / "python3",
        f'printf "%s" "$$" > "{pid_path}"\n'
        f"{sys.executable} - \"$@\" <<'PY'\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(arguments_path)!r}, 'w'))\n"
        "PY\n"
        f"exit {exit_code}\n",
    )
    process = subprocess.Popen(
        [str(entrypoint), *arguments],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(timeout=10)
    recorded_arguments = json.loads(arguments_path.read_text(encoding="utf-8"))
    recorded_pid = int(pid_path.read_text(encoding="utf-8"))
    return process, stdout, stderr, recorded_arguments, recorded_pid


def wait_for_report_phase(path: Path, phase: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.01)
            continue
        if report["phase"] == phase:
            return
        time.sleep(0.01)
    pytest.fail(f"run report did not reach phase {phase}")

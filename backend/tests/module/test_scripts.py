from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from tests.environment_entrypoint_test_support import (
    copy_environment_runtime,
    write_executable,
)


ROOT_DIR = Path(__file__).parent.parent.parent.parent
SCRIPT_NAMES = (
    "setup-backend.sh",
    "start-all.sh",
    "start-backend.sh",
    "start-frontend.sh",
    "start-voice-chat-e2e.sh",
)
LIBRARY_NAMES = ("lib/profile.sh",)
ENVIRONMENT_ENTRYPOINT_NAMES = ("up.sh", "down.sh", "verify.sh")


def test_should_keep_all_supported_script_entrypoints_executable_and_strict():
    paths = [
        *(ROOT_DIR / "scripts" / name for name in SCRIPT_NAMES),
        *(ROOT_DIR / "environments" / name for name in ENVIRONMENT_ENTRYPOINT_NAMES),
    ]

    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert path.is_file()
        assert os.access(path, os.X_OK)
        assert "set -euo pipefail" in content


def test_should_keep_all_supported_shell_entrypoints_syntax_valid():
    paths = [
        *(ROOT_DIR / "scripts" / name for name in (*SCRIPT_NAMES, *LIBRARY_NAMES)),
        *(ROOT_DIR / "environments" / name for name in ENVIRONMENT_ENTRYPOINT_NAMES),
    ]

    result = subprocess.run(
        ["bash", "-n", *map(str, paths)], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr


def test_should_keep_backend_virtual_environment_out_of_git():
    patterns = (ROOT_DIR / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".venv/" in patterns


def test_should_keep_playwright_result_directories_out_of_git():
    result_paths = (
        ROOT_DIR / "test-results" / ".last-run.json",
        ROOT_DIR / "frontend" / "test-results" / "mocked-e2e" / "evidence.json",
    )

    for result_path in result_paths:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--no-index", str(result_path)],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_should_keep_development_requirements_linked_to_runtime_requirements():
    requirements = (
        ROOT_DIR / "backend" / "requirements-dev.txt"
    ).read_text(encoding="utf-8").splitlines()

    assert "-r requirements.txt" in requirements


def test_should_start_frontend_with_mock_backend_profile(tmp_path: Path):
    scripts_dir = tmp_path / "scripts"
    (scripts_dir / "lib").mkdir(parents=True)
    shutil.copy2(ROOT_DIR / "scripts" / "start-frontend.sh", scripts_dir)
    shutil.copy2(ROOT_DIR / "scripts" / "lib" / "profile.sh", scripts_dir / "lib")
    copy_environment_runtime(tmp_path)
    (tmp_path / "frontend" / "node_modules").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "npm", 'printf "%s" "$RAG_ENABLED"\n')
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DS_PROFILE", "DS_PROFILE_REPORT"}
    }
    environment.update(
        {"DS_PROFILE": "test-mocked", "PATH": f"{bin_dir}:{environment['PATH']}"}
    )

    result = subprocess.run(
        [str(scripts_dir / "start-frontend.sh")],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "false"

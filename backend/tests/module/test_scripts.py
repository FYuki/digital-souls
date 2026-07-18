from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT_DIR = Path(__file__).parent.parent.parent.parent
SCRIPT_NAMES = (
    "setup-backend.sh",
    "start-all.sh",
    "start-backend.sh",
    "start-frontend.sh",
    "start-ollama.sh",
    "start-voice-chat-e2e.sh",
    "start-voicevox.sh",
)
LIBRARY_NAMES = ("lib/profile.sh",)


def test_should_keep_all_supported_script_entrypoints_executable_and_strict():
    for name in SCRIPT_NAMES:
        path = ROOT_DIR / "scripts" / name
        content = path.read_text(encoding="utf-8")
        assert path.is_file()
        assert os.access(path, os.X_OK)
        assert "set -euo pipefail" in content


def test_should_keep_all_supported_shell_entrypoints_syntax_valid():
    paths = [
        ROOT_DIR / "scripts" / name for name in (*SCRIPT_NAMES, *LIBRARY_NAMES)
    ]

    result = subprocess.run(
        ["bash", "-n", *map(str, paths)], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr


def test_should_keep_backend_virtual_environment_out_of_git():
    patterns = (ROOT_DIR / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".venv/" in patterns


def test_should_keep_development_requirements_linked_to_runtime_requirements():
    requirements = (
        ROOT_DIR / "backend" / "requirements-dev.txt"
    ).read_text(encoding="utf-8").splitlines()

    assert "-r requirements.txt" in requirements

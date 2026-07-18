import subprocess
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).parent.parent
ENVIRONMENTS_DIR = BACKEND_DIR.parent / "environments"


def run_python(script: str) -> None:
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_DIR,
        check=True,
        capture_output=True,
        text=True,
    )


def test_should_not_duplicate_environment_path_when_common_setup_is_reloaded() -> None:
    run_python(
        f"""
import importlib
import sys

import tests.conftest as common_setup

importlib.reload(common_setup)

assert sys.path.count({str(ENVIRONMENTS_DIR)!r}) == 1
"""
    )


def test_should_preserve_import_path_when_environment_support_is_imported() -> None:
    run_python(
        f"""
import sys

environments_dir = {str(ENVIRONMENTS_DIR)!r}
sys.path.insert(0, environments_dir)
import profile_types

sys.path[:] = [entry for entry in sys.path if entry != environments_dir]
path_before_import = list(sys.path)

import tests.environment_test_support

assert sys.path == path_before_import
"""
    )

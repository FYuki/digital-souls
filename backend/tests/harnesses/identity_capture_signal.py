from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path


root = Path(sys.argv[1])
report_path = sys.argv[2]
runtime_path = Path(sys.argv[3])
sys.path[:0] = [str(root / "environments"), str(root / "backend")]

import commands.up_command as up_command  # noqa: E402


original_current_process_identity = up_command.current_process_identity


def interrupting_current_process_identity():
    os.kill(os.getpid(), signal.SIGTERM)
    return original_current_process_identity()


up_command.current_process_identity = interrupting_current_process_identity
arguments = argparse.Namespace(
    run_report=report_path,
    profile_report=None,
    default_profile="test-mocked",
)
raise SystemExit(up_command.up_environment(root, runtime_path, arguments))

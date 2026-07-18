#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
for import_root in (ROOT_DIR, BACKEND_DIR):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from commands.down_command import down_environment
from commands.test_result_command import record_playwright_result
from commands.up_command import up_environment
from commands.verify_command import verify_environment
from commands.voicevox_command import start_voicevox
from profile_types import ProfileError


DEFAULT_RUNTIME_DIR = ROOT_DIR / ".runtime" / "environments"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Digital Souls environment lifecycle")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up")
    up.add_argument("--default-profile")
    up.add_argument("--run-report")
    up.add_argument("--profile-report")
    down = commands.add_parser("down")
    down.add_argument("--run-report")
    verify = commands.add_parser("verify")
    verify.add_argument("--default-profile")
    voicevox = commands.add_parser("voicevox")
    voicevox.add_argument("--default-profile")
    test_result = commands.add_parser("test-result")
    test_result.add_argument("--run-report", required=True)
    test_result.add_argument("--status", choices=("passed", "failed"), required=True)
    test_result.add_argument("--message", required=True)
    return parser


def _dispatch(arguments: argparse.Namespace) -> int:
    if arguments.command == "up":
        return up_environment(ROOT_DIR, DEFAULT_RUNTIME_DIR, arguments)
    if arguments.command == "down":
        return down_environment(ROOT_DIR, arguments.run_report)
    if arguments.command == "voicevox":
        return start_voicevox(ROOT_DIR, arguments.default_profile)
    if arguments.command == "test-result":
        return record_playwright_result(
            arguments.run_report, arguments.status, arguments.message
        )
    return verify_environment(ROOT_DIR, arguments.default_profile)


def main() -> int:
    try:
        return _dispatch(_parser().parse_args())
    except (ProfileError, ValueError, RuntimeError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Digital Soulsの環境Profileを検証・解決・参照するCLI。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.model_settings import MODEL_ENVIRONMENT_KEYS
from app.runtime_data_root import initialize_runtime_data_root
from app.runtime_paths import resolve_runtime_paths
from profile_report import (
    create_legacy_report,
    load_resolved_report,
    read_report_value,
)
from profile_report_store import resolve_report_paths, write_reports
from profile_resolution import resolve_profile
from profile_types import ProfileError
from profile_validation import load_profile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--profile", required=True)
    validate_report = commands.add_parser("validate-report")
    validate_report.add_argument("--report", required=True)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("--report")
    resolve.add_argument("--default-report")
    resolve.add_argument("--default-profile")
    get = commands.add_parser("get")
    get.add_argument("--report", required=True)
    get.add_argument("--path", required=True)
    commands.add_parser("model-environment-keys")
    return parser


def _resolve_command(arguments: argparse.Namespace) -> None:
    env = dict(os.environ)
    repository_root = Path(__file__).resolve().parent.parent
    runtime_paths = resolve_runtime_paths(env, repository_root)
    initialize_runtime_data_root(runtime_paths, repository_root)
    report_path, legacy_path = resolve_report_paths(
        arguments.report,
        env,
        arguments.default_report,
        runtime_paths,
    )
    report = resolve_profile(env, arguments.default_profile, runtime_paths)
    write_reports(report_path, report, legacy_path, create_legacy_report(report))
    for warning in report["compatibility"]["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(report_path)


def _get_command(arguments: argparse.Namespace) -> None:
    value = read_report_value(Path(arguments.report), arguments.path)
    if value is None:
        print("null")
    elif isinstance(value, bool):
        print(str(value).lower())
    else:
        print(value)


def _model_environment_keys_command() -> None:
    print("\n".join(MODEL_ENVIRONMENT_KEYS))


def main() -> int:
    arguments = _parser().parse_args()
    try:
        if arguments.command == "validate":
            load_profile(arguments.profile)
        elif arguments.command == "validate-report":
            load_resolved_report(Path(arguments.report))
        elif arguments.command == "resolve":
            _resolve_command(arguments)
        elif arguments.command == "get":
            _get_command(arguments)
        else:
            _model_environment_keys_command()
    except ProfileError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

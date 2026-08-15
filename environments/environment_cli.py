#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Mapping
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
for import_root in (ROOT_DIR, BACKEND_DIR):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from app.backup_restore import (
    BackupArtifactError,
    BackupIdentityError,
    BackupPublicationUncertainError,
    BackupSchemaError,
    RestoreDurabilityUncertainError,
    RestoreRecoveryRequiredError,
    RestoreSafetyError,
)
from app.backup_restore.models import BackupError
from commands.down_command import down_environment
from commands.start_command import start_environment
from commands.status_command import (
    load_environment_report,
    render_orchestrator_status,
    status_environment,
)
from commands.test_result_command import record_playwright_result
from commands.up_command import up_environment
from commands.verify_command import verify_environment
from commands.voicevox_command import start_voicevox
from environment_constants import RUN_REPORT_ENV
from environment_verification import EnvironmentVerificationError
from http_readiness import probe_http_services
from profile_resolution import resolve_dependencies
from profile_types import ProfileError
from profile_validation import load_profile
from run_report_contract import RunReportError

BACKUP_ERROR_EXIT_CODES = (
    (BackupIdentityError, 10),
    (BackupArtifactError, 11),
    (BackupSchemaError, 12),
    (RestoreSafetyError, 13),
    (BackupPublicationUncertainError, 14),
    (RestoreDurabilityUncertainError, 15),
    (RestoreRecoveryRequiredError, 16),
)
UNKNOWN_BACKUP_ERROR_MESSAGE = "backup operation failed"
UNKNOWN_ENVIRONMENT_ERROR_MESSAGE = "environment operation failed"
BACKUP_COMMANDS = ("backup", "backup-verify", "restore", "restore-verify")


def _require_readiness_url(
    service_name: str, dependency: Mapping[str, object]
) -> str:
    readiness_url = dependency.get("readinessUrl")
    if not isinstance(readiness_url, str) or not readiness_url:
        raise ProfileError(f"{service_name} readinessUrl is required")
    return readiness_url


def backup_environment(
    environment_id: str,
    repository_root: str,
    backup_root: str,
    retention_count: int,
) -> int:
    from commands.backup_restore_command import backup_environment as operation

    return operation(environment_id, repository_root, backup_root, retention_count)


def verify_environment_backup(backup_directory: str) -> int:
    from commands.backup_restore_command import verify_environment_backup as operation

    return operation(backup_directory)


def restore_environment_backup(
    environment_id: str,
    repository_root: str,
    backup_directory: str,
) -> int:
    from commands.backup_restore_command import restore_environment_backup as operation

    return operation(environment_id, repository_root, backup_directory)


def verify_restored_environment_backup(
    environment_id: str,
    repository_root: str,
    backup_directory: str,
) -> int:
    from commands.backup_restore_command import (
        verify_restored_environment_backup as operation,
    )

    return operation(environment_id, repository_root, backup_directory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Digital Souls environment lifecycle")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up")
    up.add_argument("--default-profile")
    up.add_argument("--run-report")
    up.add_argument("--profile-report")
    start = commands.add_parser("start")
    start.add_argument("--default-profile")
    start.add_argument("--run-report")
    start.add_argument("--profile-report")
    down = commands.add_parser("down")
    down.add_argument("--run-report")
    status = commands.add_parser("status")
    status.add_argument("--run-report")
    verify = commands.add_parser("verify")
    verify.add_argument("--default-profile")
    voicevox = commands.add_parser("voicevox")
    voicevox.add_argument("--default-profile")
    test_result = commands.add_parser("test-result")
    test_result.add_argument("--run-report", required=True)
    test_result.add_argument("--status", choices=("passed", "failed"), required=True)
    test_result.add_argument("--message", required=True)
    readiness = commands.add_parser("readiness")
    readiness.add_argument("--profile", required=True)
    backup, backup_verify, restore, restore_verify = (
        commands.add_parser(command) for command in BACKUP_COMMANDS
    )
    backup.add_argument("--environment", required=True)
    backup.add_argument("--repository-root", required=True)
    backup.add_argument("--backup-root", required=True)
    backup.add_argument("--retention-count", required=True, type=int)
    backup_verify.add_argument("--backup-directory", required=True)
    restore.add_argument("--environment", required=True)
    restore.add_argument("--repository-root", required=True)
    restore.add_argument("--backup-directory", required=True)
    restore_verify.add_argument("--environment", required=True)
    restore_verify.add_argument("--repository-root", required=True)
    restore_verify.add_argument("--backup-directory", required=True)
    return parser


def _dispatch(arguments: argparse.Namespace) -> int:
    if arguments.command == "backup":
        return backup_environment(
            arguments.environment,
            arguments.repository_root,
            arguments.backup_root,
            arguments.retention_count,
        )
    if arguments.command == "backup-verify":
        return verify_environment_backup(arguments.backup_directory)
    if arguments.command == "restore":
        return restore_environment_backup(
            arguments.environment,
            arguments.repository_root,
            arguments.backup_directory,
        )
    if arguments.command == "restore-verify":
        return verify_restored_environment_backup(
            arguments.environment,
            arguments.repository_root,
            arguments.backup_directory,
        )
    if arguments.command == "up":
        return up_environment(ROOT_DIR, arguments)
    if arguments.command == "start":
        return start_environment(ROOT_DIR, arguments)
    if arguments.command == "down":
        return down_environment(ROOT_DIR, arguments.run_report)
    if arguments.command == "status":
        return status_environment(ROOT_DIR, arguments.run_report)
    if arguments.command == "voicevox":
        return start_voicevox(ROOT_DIR, arguments.default_profile)
    if arguments.command == "test-result":
        return record_playwright_result(
            arguments.run_report, arguments.status, arguments.message
        )
    if arguments.command == "readiness":
        configured_report = os.environ.get(RUN_REPORT_ENV)
        if configured_report is not None:
            report = load_environment_report(ROOT_DIR, configured_report)
            print(render_orchestrator_status(report))
        profile = load_profile(arguments.profile)
        dependencies = resolve_dependencies(profile["dependencies"])
        service_urls = {
            "frontend": _require_readiness_url("frontend", dependencies["frontend"]),
            "backend": _require_readiness_url("backend", dependencies["backend"]),
        }
        services, ready = probe_http_services(service_urls, timeout_seconds=2.0)
        report = {
            "status": "ready" if ready else "not_ready",
            "profile": profile["name"],
            "services": services,
        }
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 0 if ready else 1
    return verify_environment(ROOT_DIR, arguments.default_profile)


def main() -> int:
    arguments = _parser().parse_args()
    try:
        return _dispatch(arguments)
    except BackupError as error:
        for error_type, exit_code in BACKUP_ERROR_EXIT_CODES:
            if isinstance(error, error_type):
                print(f"ERROR: {error.public_message}", file=sys.stderr)
                return exit_code
        print(f"ERROR: {UNKNOWN_BACKUP_ERROR_MESSAGE}", file=sys.stderr)
        return 1
    except (EnvironmentVerificationError, ProfileError, RunReportError):
        print(f"ERROR: {UNKNOWN_ENVIRONMENT_ERROR_MESSAGE}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - CLI境界で診断方針を操作種別ごとに固定する
        if arguments.command in BACKUP_COMMANDS:
            print(f"ERROR: {UNKNOWN_ENVIRONMENT_ERROR_MESSAGE}", file=sys.stderr)
        else:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

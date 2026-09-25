from __future__ import annotations

import json
import os
import pwd
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI

from tests.dogfood_infrastructure_test_support import (
    DOGFOOD_SCRIPTS_DIR,
    TEST_REVISION,
    TEST_DEPLOYMENT_IMAGES,
    TEST_SECRET_SENTINEL,
    TEST_SERVICE_GROUP,
    command_with_root_owned_revision,
    read_valid_deployment_manifest,
    render_dogfood_assets,
    write_dogfood_env,
    write_dogfood_revision,
    write_executable,
)
from tests.environment_entrypoint_test_support import copy_environment_runtime


NEXT_REVISION = "89abcdef0123456789abcdef0123456789abcdef"

THIRD_REVISION = "fedcba9876543210fedcba9876543210fedcba98"

CONVERSATION_SENTINEL = "会話本文をdeploymentへ出力してはならない"

PROMPT_SENTINEL = "promptをdeploymentへ出力してはならない"

ROOT_OPERATION_CASES = (
    ("bootstrap.sh", ()),
    ("migrate-deployment-contract.sh", ("--commit", NEXT_REVISION)),
    ("deploy.sh", ("--commit", NEXT_REVISION)),
    ("rollback.sh", ("--to", NEXT_REVISION)),
)

ROOT_GUARD_POST_COMMANDS = (
    "chmod",
    "chown",
    "docker",
    "getent",
    "git",
    "gpasswd",
    "groupadd",
    "install",
    "npm",
    "systemctl",
    "useradd",
    "usermod",
)

def _manifest_payload(
    previous_commit: object,
    target_commit: object,
    *,
    data_schema_version: object = 3,
) -> dict[str, object]:
    return {
        "previousCommit": previous_commit,
        "targetCommit": target_commit,
        "profileSchemaVersion": 1,
        "dataSchemaVersion": data_schema_version,
        "backupId": "backup-current",
        "images": TEST_DEPLOYMENT_IMAGES,
        "deployedAt": "2026-07-31T00:00:00Z",
    }

def _legacy_manifest_payload(
    previous_commit: object = TEST_REVISION,
    target_commit: object = NEXT_REVISION,
) -> dict[str, object]:
    payload = _manifest_payload(previous_commit, target_commit)
    del payload["images"]
    return payload

def _install_rejection_fakes(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "external.calls"
    for command in ("git", "sudo", "npm", "systemctl", "install", "chown", "chmod"):
        write_executable(
            bin_dir / command,
            f'printf "%s\\t%s\\n" "{command}" "$*" >> "{log_path}"\nexit 99\n',
        )
    write_executable(
        bin_dir / "id",
        'if [ "${1-}" = "-u" ]; then printf "0\\n"; else exit 99; fi\n',
    )
    return bin_dir, log_path

def _prepare_deploy_scenario(
    tmp_path: Path,
    *,
    failure: str | None = None,
    generation_count: int = 0,
    backup_output: str | None = None,
    database_exists: bool = True,
    private_sentinels: bool = True,
    target_revision: str = NEXT_REVISION,
    head_revision: str = TEST_REVISION,
    current_deployment_revision: str | None = None,
    deployment_revision: str | None = TEST_REVISION,
    current_manifest_payload: dict[str, object] | str | None = None,
) -> tuple[dict[str, str], Path]:
    env_path, data_dir = write_dogfood_env(tmp_path)
    (tmp_path / "backups" / "backup-20260906T000000Z-0123456789ab-abcdef012345").mkdir(
        parents=True
    )
    env_path.write_text(
        env_path.read_text(encoding="utf-8")
        + f"\nDOGFOOD_SERVICE_HOME_DIR={tmp_path / 'service-home'}\n",
        encoding="utf-8",
    )
    clone_dir = tmp_path / "clone"
    (clone_dir / ".git").mkdir(parents=True)
    call_log = tmp_path / "deploy.calls"
    head_path = tmp_path / "head"
    head_read_count_path = tmp_path / "head-read-count"
    checkout_count_path = tmp_path / "checkout-count"
    head_path.write_text(head_revision, encoding="utf-8")
    setup_backend = clone_dir / "scripts" / "setup-backend.sh"
    setup_backend.parent.mkdir(parents=True)
    write_executable(
        setup_backend,
        f'printf "backend-setup\\n" >> "{call_log}"\n'
        '[ "${DEPLOY_FAILURE-}" != "backend-setup" ]\n',
    )
    restart = clone_dir / "scripts" / "dogfood" / "restart-services.sh"
    restart.parent.mkdir(parents=True)
    write_executable(
        restart,
        f'printf "restart\\n" >> "{call_log}"\n'
        '[ "${DEPLOY_FAILURE-}" != "restart" ]\n',
    )
    cli = clone_dir / "environments" / "environment_cli.py"
    cli.parent.mkdir(parents=True)
    write_executable(
        cli,
        f'printf "cli\\t%s\\n" "$*" >> "{call_log}"\n'
        f'printf "cli-home\\t%s\\t%s\\n" "$1" "$HOME" >> "{call_log}"\n'
        f'printf "cli-gitconfig\\t%s\\t%s\\n" "$1" "${{GIT_CONFIG_GLOBAL-}}" >> "{call_log}"\n'
        'case "$1" in\n'
        '  backup) [ "${DEPLOY_FAILURE-}" != "backup" ]; '
        "printf '%s\\n' \"$DEPLOY_BACKUP_OUTPUT\" ;;\n"
        '  backup-verify) [ "${DEPLOY_FAILURE-}" != "verify" ] || exit 1; '
        "printf '%s\\n' "
        "'{\"status\":\"ok\",\"artifacts\":[]}' ;;\n"
        f'  wait-readiness) count=$(cat "{tmp_path / "readiness-count"}" 2>/dev/null || printf 0); '
        f'count=$((count + 1)); printf "%s" "$count" > "{tmp_path / "readiness-count"}"; '
        'case "${DEPLOY_FAILURE-}" in '
        f'readiness) if [ "$count" -gt 1 ]; then printf "readiness-result\\tsuccess\\n" >> "{call_log}"; '
        f'else printf "readiness-result\\tfailure\\n" >> "{call_log}"; false; fi ;; '
        f'rollback-readiness|rollback-checkout|rollback-readiness-head-unavailable) '
        f'printf "readiness-result\\tfailure\\n" >> "{call_log}"; false ;; '
        f'rollback-readiness-revision-unavailable) if [ "$count" -gt 1 ]; then '
        f'rm -f "{tmp_path / "config" / "dogfood.revision"}"; fi; '
        f'printf "readiness-result\\tfailure\\n" >> "{call_log}"; false ;; '
        f'*) printf "readiness-result\\tsuccess\\n" >> "{call_log}" ;; esac ;;\n'
        "esac\n",
    )
    python = clone_dir / "backend" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    write_executable(python, 'exec "$@"\n')
    profile = clone_dir / "environments" / "profiles" / "dogfood.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"schemaVersion": 1}), encoding="utf-8")
    database = data_dir / "conversation-history.db"
    if database_exists:
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA user_version = 3")
    if private_sentinels:
        (data_dir / "private-sentinels").write_text(
            f"{CONVERSATION_SENTINEL}\n{PROMPT_SENTINEL}\n",
            encoding="utf-8",
        )
    (tmp_path / "log").mkdir()
    deployments = tmp_path / "state" / "deployments"
    deployments.mkdir(parents=True)
    (tmp_path / "state").chmod(0o750)
    deployments.chmod(0o750)
    for index in range(generation_count):
        revision = f"{index + 1:040x}"
        generation = deployments / f"202607{index + 1:02d}T000000Z-{revision[:12]}.json"
        generation.write_text(
            json.dumps(
                {
                    "previousCommit": TEST_REVISION,
                    "targetCommit": revision,
                    "profileSchemaVersion": 1,
                    "dataSchemaVersion": 3,
                    "backupId": f"backup-{index + 1}",
                    "images": TEST_DEPLOYMENT_IMAGES,
                    "deployedAt": f"2026-07-{index + 1:02d}T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        generation.chmod(0o640)
    if (
        current_deployment_revision is not None
        and current_manifest_payload is not None
    ):
        raise ValueError(
            "current_deployment_revisionとcurrent_manifest_payloadは"
            "同時に指定できません"
        )
    if current_deployment_revision is not None:
        current_manifest_payload = _manifest_payload(
            TEST_REVISION,
            current_deployment_revision,
        )
    if current_manifest_payload is not None:
        current_manifest = deployments / "current.json"
        current_manifest.write_text(
            current_manifest_payload
            if isinstance(current_manifest_payload, str)
            else json.dumps(current_manifest_payload),
            encoding="utf-8",
        )
        current_manifest.chmod(0o640)

    revision_path = tmp_path / "config" / "dogfood.revision"
    if deployment_revision is None:
        revision_path.unlink()
    else:
        revision_path.write_text(f"{deployment_revision}\n", encoding="utf-8")
        revision_path.chmod(0o640)

    bin_dir = tmp_path / "deploy-bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "id", 'printf "0\\n"\n')
    write_executable(
        bin_dir / "git",
        f'printf "git\\t%s\\n" "$*" >> "{call_log}"\n'
        'case "$*" in\n'
        '  *"remote get-url origin"*) printf "%s\\n" "$DOGFOOD_REPOSITORY_URL" ;;\n'
        '  *"status --porcelain"*) [ "${DEPLOY_FAILURE-}" != "dirty" ] || printf " M dirty\\n" ;;\n'
        '  *"merge-base --is-ancestor"*) [ "${DEPLOY_FAILURE-}" != "unresolved" ] ;;\n'
        '  *"rev-parse --is-shallow-repository"*) printf "false\\n" ;;\n'
        f'  *"rev-parse HEAD"*) count=$(cat "{head_read_count_path}" 2>/dev/null || printf 0); '
        f'count=$((count + 1)); printf "%s" "$count" > "{head_read_count_path}"; '
        '[ "${DEPLOY_FAILURE-}" != "rollback-readiness-head-unavailable" ] '
        f'|| [ "$count" -le 3 ]; cat "{head_path}" ;;\n'
        '  *"rev-parse --verify"*) printf "%s\\n" "$DEPLOY_TARGET" ;;\n'
        '  *"symbolic-ref --quiet HEAD"*) exit 1 ;;\n'
        f'  *"checkout --detach"*) count=$(cat "{checkout_count_path}" 2>/dev/null || printf 0); '
        f'count=$((count + 1)); printf "%s" "$count" > "{checkout_count_path}"; '
        '[ "${DEPLOY_FAILURE-}" != "rollback-checkout" ] || [ "$count" -le 1 ]; '
        f'printf "%s" "${{@: -1}}" > "{head_path}" ;;\n'
        "esac\n",
    )
    write_executable(
        bin_dir / "sudo",
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in --preserve-env=*) shift ;; -u) shift 2 ;; *) break ;; esac\n'
        'done\nexec "$@"\n',
    )
    write_executable(
        bin_dir / "docker",
        f'printf "docker\\t%s\\n" "$*" >> "{call_log}"\n'
        'case "$*" in\n'
        f'  *"digital-souls-backend:"*) printf "sha256:{"1" * 64}\\n" ;;\n'
        f'  *"digital-souls-frontend:"*) printf "sha256:{"2" * 64}\\n" ;;\n'
        f'  *"digital-souls-whisper:"*) printf "sha256:{"3" * 64}\\n" ;;\n'
        'esac\n',
    )
    write_executable(
        bin_dir / "npm",
        f'printf "frontend-build\\t%s\\n" "$*" >> "{call_log}"\n'
        '[ "${DEPLOY_FAILURE-}" != "frontend-build" ]\n',
    )
    write_executable(
        bin_dir / "install",
        f'printf "install\\t%s\\n" "$*" >> "{call_log}"\n'
        'arguments=()\nwhile [ "$#" -gt 0 ]; do\n'
        '  case "$1" in -o|-g) shift 2 ;; *) arguments+=("$1"); shift ;; esac\n'
        "done\n"
        'destination="${arguments[${#arguments[@]}-1]}"\n'
        'case "$destination" in\n'
        f'  "{tmp_path / "config"}/.dogfood.revision.ready."*) printf "revision-update\\n" >> "{call_log}" ;;\n'
        f'  "{tmp_path / "state" / "deployments"}/.manifest.ready."*) printf "manifest-write\\n" >> "{call_log}" ;;\n'
        "esac\n"
        '/usr/bin/install "${arguments[@]}"\n'
        'case "$destination" in\n'
        f'  "{tmp_path / "config"}/.dogfood.revision.ready."*) '
        f'[ "${{DEPLOY_SKIP_FAKE_REVISION_CHOWN-}}" = "1" ] '
        f'|| /usr/bin/chown "0:$DOGFOOD_SERVICE_GROUP" "$destination" ;;\n'
        'esac\n',
    )
    for command in ("chown", "chmod"):
        write_executable(
            bin_dir / command,
            f'printf "{command}\\t%s\\n" "$*" >> "{call_log}"\n'
            f'[ "${{DEPLOY_FAILURE-}}" != "{command}" ]\n',
        )
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DOGFOOD_ENV_FILE": str(env_path),
        "WSL_DISTRO_NAME": "Ubuntu-dogfood",
        "DEPLOY_TARGET": target_revision,
        "DEPLOY_BACKUP_OUTPUT": backup_output
        if backup_output is not None
        else json.dumps(
            {
                "status": "ok",
                "backupDirectory": str(
                    tmp_path / "backups" / "backup-20260906T000000Z-0123456789ab-abcdef012345"
                ),
            }
        ),
    }
    if failure is not None:
        environment["DEPLOY_FAILURE"] = failure
    return environment, call_log

def _invoke_deployment_contract_migration(
    tmp_path: Path,
    environment: dict[str, str],
    *,
    target_revision: str = NEXT_REVISION,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command_with_root_owned_revision(
            tmp_path / "config" / "dogfood.revision",
            [
                str(DOGFOOD_SCRIPTS_DIR / "migrate-deployment-contract.sh"),
                "--commit",
                target_revision,
            ],
        ),
        env={**environment, "DEPLOY_SKIP_FAKE_REVISION_CHOWN": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )

def _run_deploy(
    tmp_path: Path,
    *,
    failure: str | None = None,
    no_auto_rollback: bool = False,
    generation_count: int = 0,
    backup_output: str | None = None,
    database_exists: bool = True,
    target_revision: str = NEXT_REVISION,
    head_revision: str = TEST_REVISION,
    current_deployment_revision: str | None = None,
    deployment_revision: str | None = TEST_REVISION,
    current_manifest_payload: dict[str, object] | str | None = None,
) -> tuple[subprocess.CompletedProcess[str], tuple[str, ...]]:
    environment, call_log = _prepare_deploy_scenario(
        tmp_path,
        failure=failure,
        generation_count=generation_count,
        backup_output=backup_output,
        database_exists=database_exists,
        target_revision=target_revision,
        head_revision=head_revision,
        current_deployment_revision=current_deployment_revision,
        deployment_revision=deployment_revision,
        current_manifest_payload=current_manifest_payload,
    )
    arguments = ["--commit", target_revision]
    if no_auto_rollback:
        arguments.append("--no-auto-rollback")
    result = _invoke_deploy(
        tmp_path,
        environment,
        arguments,
        deployment_revision is not None,
    )
    calls = (
        tuple(call_log.read_text(encoding="utf-8").splitlines())
        if call_log.exists()
        else ()
    )
    return result, calls

def _invoke_deploy(
    tmp_path: Path,
    environment: dict[str, str],
    arguments: list[str],
    revision_exists: bool,
) -> subprocess.CompletedProcess[str]:
    command = [str(DOGFOOD_SCRIPTS_DIR / "deploy.sh"), *arguments]
    if revision_exists:
        command = command_with_root_owned_revision(
            tmp_path / "config" / "dogfood.revision", command
        )
    else:
        command = ["fakeroot", *command]
    return subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

def _read_log_records(log_dir: Path) -> tuple[str, ...]:
    return tuple(
        path.read_text(encoding="utf-8")
        for path in log_dir.rglob("*")
        if path.is_file()
    )

def _run_service_git_trust_convergence(
    service_home: Path,
    clone_dir: Path,
    environment: dict[str, str],
    *,
    repeat: bool,
) -> subprocess.CompletedProcess[str]:
    service_user = pwd.getpwuid(os.getuid()).pw_name
    convergence_calls = (
        "dogfood_converge_service_git_trust; " if repeat else ""
    ) + "dogfood_converge_service_git_trust"
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; DOGFOOD_SERVICE_HOME_DIR=$2; DOGFOOD_CLONE_DIR=$3; '
            "DOGFOOD_SERVICE_USER=$4; DOGFOOD_SERVICE_GROUP=$5; " + convergence_calls,
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "deployment-lib.sh"),
            str(service_home),
            str(clone_dir),
            service_user,
            TEST_SERVICE_GROUP,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

def _read_effective_global_safe_directories(
    service_home: Path,
    clone_dir: Path,
) -> list[str]:
    return subprocess.run(
        [
            "git",
            "-C",
            str(clone_dir),
            "config",
            "--global",
            "--includes",
            "--get-all",
            "safe.directory",
        ],
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(service_home),
            "GIT_CONFIG_GLOBAL": str(service_home / ".gitconfig"),
            "XDG_CONFIG_HOME": str(service_home / ".config"),
        },
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

from __future__ import annotations

import json
import os
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
CONVERSATION_SENTINEL = "会話本文をdeploymentへ出力してはならない"
PROMPT_SENTINEL = "promptをdeploymentへ出力してはならない"
ROOT_OPERATION_CASES = (
    ("bootstrap.sh", ()),
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


def test_should_check_current_profile_readiness_without_starting_processes(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    environments_dir = copy_environment_runtime(runtime_root)
    process_start_attempt = tmp_path / "process-start.attempt"
    (environments_dir / "commands" / "up_command.py").write_text(
        "from pathlib import Path\n\n"
        "def up_environment(*_args: object, **_kwargs: object) -> int:\n"
        f"    Path({str(process_start_attempt)!r}).touch()\n"
        '    raise AssertionError("readiness must not start processes")\n',
        encoding="utf-8",
    )
    probe_log = tmp_path / "readiness-probes.json"
    readiness_module = environments_dir / "http_readiness.py"
    readiness_module.write_text(
        readiness_module.read_text(encoding="utf-8")
        + "\n\nimport json\nimport os\nfrom pathlib import Path\n\n"
        + "def probe_http(url: str, *, timeout_seconds: float) -> ReadinessResult:\n"
        + '    path = Path(os.environ["READINESS_PROBE_LOG"])\n'
        + '    observations = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []\n'
        + '    observations.append({"url": url, "timeoutSeconds": timeout_seconds})\n'
        + '    path.write_text(json.dumps(observations), encoding="utf-8")\n'
        + '    return ReadinessResult(url, 1, 0.0, "ready")\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(environments_dir / "environment_cli.py"),
            "readiness",
            "--profile",
            "dogfood",
        ],
        env={**os.environ, "READINESS_PROBE_LOG": str(probe_log)},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert not process_start_attempt.exists()
    assert result.returncode == 0, (result.stdout, result.stderr)
    report = json.loads(result.stdout)
    assert report["status"] == "ready"
    assert report["profile"] == "dogfood"
    assert set(report["services"]) == {"backend", "frontend"}
    observations = json.loads(probe_log.read_text(encoding="utf-8"))
    assert observations == [
        {"url": "http://localhost:15173/", "timeoutSeconds": 2.0},
        {"url": "http://localhost:18000/", "timeoutSeconds": 2.0},
    ]


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
) -> tuple[dict[str, str], Path]:
    env_path, data_dir = write_dogfood_env(tmp_path)
    clone_dir = tmp_path / "clone"
    (clone_dir / ".git").mkdir(parents=True)
    call_log = tmp_path / "deploy.calls"
    head_path = tmp_path / "head"
    head_read_count_path = tmp_path / "head-read-count"
    checkout_count_path = tmp_path / "checkout-count"
    head_path.write_text(TEST_REVISION, encoding="utf-8")
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
        'case "$1" in\n'
        '  backup) [ "${DEPLOY_FAILURE-}" != "backup" ]; '
        "printf '%s\\n' \"$DEPLOY_BACKUP_OUTPUT\" ;;\n"
        '  backup-verify) [ "${DEPLOY_FAILURE-}" != "verify" ] ;;\n'
        f'  readiness) count=$(cat "{tmp_path / "readiness-count"}" 2>/dev/null || printf 0); '
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
                    "deployedAt": f"2026-07-{index + 1:02d}T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        generation.chmod(0o640)

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
        f'/usr/bin/chown "0:{os.getgid()}" "$destination" ;;\n'
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
        "DEPLOY_TARGET": NEXT_REVISION,
        "DEPLOY_BACKUP_OUTPUT": backup_output
        if backup_output is not None
        else json.dumps(
            {
                "status": "ok",
                "backupDirectory": str(tmp_path / "backups" / "backup-test-generation"),
            }
        ),
    }
    if failure is not None:
        environment["DEPLOY_FAILURE"] = failure
    return environment, call_log


def _run_deploy(
    tmp_path: Path,
    *,
    failure: str | None = None,
    no_auto_rollback: bool = False,
    generation_count: int = 0,
    backup_output: str | None = None,
    database_exists: bool = True,
) -> tuple[subprocess.CompletedProcess[str], tuple[str, ...]]:
    environment, call_log = _prepare_deploy_scenario(
        tmp_path,
        failure=failure,
        generation_count=generation_count,
        backup_output=backup_output,
        database_exists=database_exists,
    )
    arguments = ["--commit", NEXT_REVISION]
    if no_auto_rollback:
        arguments.append("--no-auto-rollback")
    result = subprocess.run(
        command_with_root_owned_revision(
            tmp_path / "config" / "dogfood.revision",
            [str(DOGFOOD_SCRIPTS_DIR / "deploy.sh"), *arguments],
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = (
        tuple(call_log.read_text(encoding="utf-8").splitlines())
        if call_log.exists()
        else ()
    )
    return result, calls


def _read_log_records(log_dir: Path) -> tuple[str, ...]:
    return tuple(
        path.read_text(encoding="utf-8")
        for path in log_dir.rglob("*")
        if path.is_file()
    )


def test_should_deploy_only_after_backup_verify_and_record_a_safe_manifest(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(tmp_path)

    assert result.returncode == 0, (result.stdout, result.stderr)
    backend_setups = tuple(
        index for index, call in enumerate(calls) if call == "backend-setup"
    )
    assert len(backend_setups) == 2
    operations = (
        backend_setups[0],
        *(
            next(index for index, call in enumerate(calls) if marker in call)
            for marker in (
                "cli\tbackup ",
                "cli\tbackup-verify ",
                "manifest-write",
                "revision-update",
                "checkout --detach",
            )
        ),
        backend_setups[1],
        *(
            next(index for index, call in enumerate(calls) if marker in call)
            for marker in (
                "frontend-build",
                "restart",
                "cli\treadiness ",
            )
        ),
    )
    assert operations == tuple(sorted(operations))
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{NEXT_REVISION}\n"
    generations = tuple((tmp_path / "state" / "deployments").glob("*.json"))
    generation = next(path for path in generations if path.name != "current.json")
    read_valid_deployment_manifest(
        generation,
        {
            "previousCommit": TEST_REVISION,
            "targetCommit": NEXT_REVISION,
            "profileSchemaVersion": 1,
            "dataSchemaVersion": 3,
            "backupId": str(tmp_path / "backups" / "backup-test-generation"),
        },
    )
    assert any(
        "install\t" in call
        and "-m 0640" in call
        and "-o root" in call
        and f"-g {TEST_SERVICE_GROUP}" in call
        and "/.manifest.ready." in call
        for call in calls
    )


def test_should_reject_missing_database_without_deployment_side_effects(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(tmp_path, database_exists=False)

    assert result.returncode != 0
    assert calls.count("backend-setup") == 0
    assert not any(
        call.startswith("git\t") and " fetch " in call for call in calls
    )
    assert not any(call.startswith("cli\tbackup ") for call in calls)
    assert tuple((tmp_path / "state" / "deployments").glob("*.json")) == ()
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{TEST_REVISION}\n"
    assert (tmp_path / "head").read_text(encoding="utf-8") == TEST_REVISION
    assert not any("checkout --detach" in call for call in calls)
    assert "digital-souls-dogfood.target" in result.stderr
    assert "conversation-history.db" in result.stderr


@pytest.mark.anyio
async def test_should_create_database_on_application_start_before_deploy_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    environment, call_log = _prepare_deploy_scenario(
        tmp_path,
        database_exists=False,
        private_sentinels=False,
    )
    generated_dir = tmp_path / "generated"
    render_dogfood_assets(
        tmp_path / "dogfood.env",
        tmp_path / "config" / "dogfood.revision",
        generated_dir,
    )
    application_unit_path = generated_dir / "digital-souls-application.service"
    assert application_unit_path.is_file()
    application_unit = application_unit_path.read_text(encoding="utf-8")
    data_dir = tmp_path / "data"

    assert not (data_dir / "conversation-history.db").exists()
    assert (
        f"ExecStart={tmp_path / 'clone' / 'environments' / 'up.sh'}"
        in application_unit
    )
    assert f"DS_DATA_DIR={data_dir}" in application_unit
    monkeypatch.setenv("DS_ENVIRONMENT_ID", "dogfood")
    monkeypatch.setenv("DS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("RAG_ENABLED", "false")
    async with main.lifespan(FastAPI()):
        assert (data_dir / "conversation-history.db").is_file()

    result = subprocess.run(
        command_with_root_owned_revision(
            tmp_path / "config" / "dogfood.revision",
            [str(DOGFOOD_SCRIPTS_DIR / "deploy.sh"), "--commit", NEXT_REVISION],
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = tuple(call_log.read_text(encoding="utf-8").splitlines())

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert calls.count("backend-setup") == 2


def test_should_not_expose_private_content_in_deploy_outputs(
    tmp_path: Path,
) -> None:
    result, _ = _run_deploy(tmp_path)

    assert result.returncode == 0, (result.stdout, result.stderr)
    deployments = tmp_path / "state" / "deployments"
    manifest_records = tuple(
        path.read_text(encoding="utf-8") for path in deployments.glob("*.json")
    )
    observations = (
        result.stdout,
        result.stderr,
        *manifest_records,
        *_read_log_records(tmp_path / "log"),
    )
    for observation in observations:
        for sentinel in (
            TEST_SECRET_SENTINEL,
            CONVERSATION_SENTINEL,
            PROMPT_SENTINEL,
        ):
            assert sentinel not in observation


@pytest.mark.parametrize("failure", ("dirty", "unresolved", "backup", "verify"))
def test_should_stop_before_checkout_when_a_deploy_gate_fails(
    tmp_path: Path,
    failure: str,
) -> None:
    result, calls = _run_deploy(tmp_path, failure=failure)

    assert result.returncode != 0
    assert not any("checkout --detach" in call for call in calls)
    expected_backend_setups = 0 if failure == "dirty" else 1
    assert calls.count("backend-setup") == expected_backend_setups
    assert "restart" not in calls
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{TEST_REVISION}\n"


@pytest.mark.parametrize(
    ("failure", "last_operation"),
    (
        ("frontend-build", "frontend-build"),
        ("chown", "chown\t"),
        ("chmod", "chmod\t"),
        ("restart", "restart"),
    ),
)
def test_should_stop_deploy_at_the_first_activation_failure(
    tmp_path: Path,
    failure: str,
    last_operation: str,
) -> None:
    result, calls = _run_deploy(tmp_path, failure=failure)

    assert result.returncode != 0
    assert any(last_operation in call for call in calls)
    assert not any(call.startswith("cli\treadiness ") for call in calls)
    diagnostic = result.stdout + result.stderr
    assert f"現在のrevision: {NEXT_REVISION}" in diagnostic
    assert f"現在のHEAD: {NEXT_REVISION}" in diagnostic


def test_should_stop_before_backup_when_current_backend_setup_fails(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(tmp_path, failure="backend-setup")

    assert result.returncode != 0
    assert calls.count("backend-setup") == 1
    assert not any(call.startswith("cli\tbackup ") for call in calls)
    assert not tuple((tmp_path / "state" / "deployments").glob("*.json"))
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{TEST_REVISION}\n"
    assert not any("checkout --detach" in call for call in calls)


@pytest.mark.parametrize("unsafe_entry", ("deployments", "manifest"))
def test_should_reject_unsafe_deployment_storage_before_deploy_side_effects(
    tmp_path: Path,
    unsafe_entry: str,
) -> None:
    environment, call_log = _prepare_deploy_scenario(tmp_path)
    deployments = tmp_path / "state" / "deployments"
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("unchanged", encoding="utf-8")
    if unsafe_entry == "deployments":
        for path in deployments.iterdir():
            path.unlink()
        deployments.rmdir()
        deployments.symlink_to(outside, target_is_directory=True)
    else:
        (deployments / "current.json").symlink_to(marker)

    result = subprocess.run(
        command_with_root_owned_revision(
            tmp_path / "config" / "dogfood.revision",
            [str(DOGFOOD_SCRIPTS_DIR / "deploy.sh"), "--commit", NEXT_REVISION],
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert not call_log.exists()
    assert marker.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize("unsafe_parent", ("symlink", "non-root-owner"))
def test_should_reject_an_unsafe_manifest_parent_before_deploy_side_effects(
    tmp_path: Path,
    unsafe_parent: str,
) -> None:
    environment, call_log = _prepare_deploy_scenario(tmp_path)
    env_path = Path(environment["DOGFOOD_ENV_FILE"])
    manifest_root = tmp_path / "manifest-root"
    manifest_root.mkdir()
    intermediate = manifest_root / "intermediate"
    command = [str(DOGFOOD_SCRIPTS_DIR / "deploy.sh"), "--commit", NEXT_REVISION]
    if unsafe_parent == "symlink":
        intermediate.symlink_to(tmp_path, target_is_directory=True)
    else:
        intermediate.mkdir()
        (tmp_path / "state").rename(intermediate / "state")
        command = [
            "fakeroot",
            "bash",
            "-c",
            'owner=$1; shift; /usr/bin/chown 1234 "$owner"; exec "$@"',
            "bash",
            str(intermediate),
            *command,
        ]
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            f"DOGFOOD_STATE_DIR={tmp_path / 'state'}",
            f"DOGFOOD_STATE_DIR={intermediate / 'state'}",
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert not call_log.exists()


def test_should_stop_before_external_side_effects_when_configuration_is_missing(
    tmp_path: Path,
) -> None:
    environment, call_log = _prepare_deploy_scenario(tmp_path)
    env_path = Path(environment["DOGFOOD_ENV_FILE"])
    env_path.write_text(
        "\n".join(
            line
            for line in env_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith("DOGFOOD_STATE_DIR=")
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(DOGFOOD_SCRIPTS_DIR / "deploy.sh"), "--commit", NEXT_REVISION],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert not call_log.exists()


@pytest.mark.parametrize(
    "backup_output",
    (
        "not-json",
        json.dumps({"status": "ok"}),
        json.dumps({"status": "ok", "backupDirectory": 42}),
        json.dumps({"status": "ok", "backupDirectory": ""}),
    ),
    ids=(
        "invalid-json",
        "missing-directory",
        "non-string-directory",
        "empty-directory",
    ),
)
def test_should_stop_before_backup_verify_when_backup_output_breaks_its_contract(
    tmp_path: Path,
    backup_output: str,
) -> None:
    result, calls = _run_deploy(tmp_path, backup_output=backup_output)

    assert result.returncode != 0
    assert any(call.startswith("cli\tbackup ") for call in calls)
    assert not any(call.startswith("cli\tbackup-verify ") for call in calls)
    assert not any("checkout --detach" in call for call in calls)
    assert calls.count("backend-setup") == 1
    assert "restart" not in calls
    assert not tuple((tmp_path / "state" / "deployments").glob("*.json"))
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{TEST_REVISION}\n"


def test_should_automatically_restore_the_previous_revision_after_readiness_failure(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(tmp_path, failure="readiness")

    assert result.returncode != 0
    activation_markers = (
        ("checkout --detach", "checkout"),
        ("backend-setup", "backend-setup"),
        ("frontend-build", "frontend-build"),
        ("chown\t", "chown"),
        ("chmod\t", "chmod"),
        ("restart", "restart"),
        ("cli\treadiness ", "readiness"),
        ("readiness-result\tfailure", "readiness-failure"),
        ("readiness-result\tsuccess", "readiness-success"),
    )
    activation_operations = tuple(
        operation
        for call in calls
        for marker, operation in activation_markers
        if marker in call
    )
    assert activation_operations == (
        "backend-setup",
        "checkout",
        "backend-setup",
        "frontend-build",
        "chown",
        "chmod",
        "restart",
        "readiness",
        "readiness-failure",
        "checkout",
        "backend-setup",
        "frontend-build",
        "chown",
        "chmod",
        "restart",
        "readiness",
        "readiness-success",
    )
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{TEST_REVISION}\n"


def test_should_not_rollback_when_readiness_failure_is_explicitly_suppressed(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(
        tmp_path,
        failure="readiness",
        no_auto_rollback=True,
    )

    assert result.returncode != 0
    assert sum("checkout --detach" in call for call in calls) == 1
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{NEXT_REVISION}\n"


def test_should_report_observed_state_when_automatic_rollback_fails(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(tmp_path, failure="rollback-readiness")

    assert result.returncode != 0
    assert sum("checkout --detach" in call for call in calls) == 2
    diagnostic = result.stdout + result.stderr
    revision = (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ).strip()
    head = (tmp_path / "head").read_text(encoding="utf-8")
    assert f"現在のrevision: {revision}" in diagnostic
    assert f"現在のHEAD: {head}" in diagnostic
    assert TEST_SECRET_SENTINEL not in diagnostic
    assert CONVERSATION_SENTINEL not in diagnostic
    assert PROMPT_SENTINEL not in diagnostic


def test_should_report_distinct_observed_state_after_partial_rollback_failure(
    tmp_path: Path,
) -> None:
    result, _ = _run_deploy(tmp_path, failure="rollback-checkout")

    assert result.returncode != 0
    diagnostic = result.stdout + result.stderr
    revision = (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ).strip()
    head = (tmp_path / "head").read_text(encoding="utf-8")
    assert revision != head
    assert f"現在のrevision: {revision}" in diagnostic
    assert f"現在のHEAD: {head}" in diagnostic


@pytest.mark.parametrize(
    ("failure", "unavailable", "available"),
    (
        (
            "rollback-readiness-revision-unavailable",
            "現在のrevision: 取得不能",
            f"現在のHEAD: {TEST_REVISION}",
        ),
        (
            "rollback-readiness-head-unavailable",
            "現在のHEAD: 取得不能",
            f"現在のrevision: {TEST_REVISION}",
        ),
    ),
    ids=("revision", "head"),
)
def test_should_report_each_unavailable_state_observation_independently(
    tmp_path: Path,
    failure: str,
    unavailable: str,
    available: str,
) -> None:
    result, _ = _run_deploy(tmp_path, failure=failure)

    assert result.returncode != 0
    diagnostic = result.stdout + result.stderr
    assert unavailable in diagnostic
    assert available in diagnostic


def test_should_keep_only_the_twenty_newest_deployment_generations(
    tmp_path: Path,
) -> None:
    result, _ = _run_deploy(tmp_path, generation_count=20)

    assert result.returncode == 0, (result.stdout, result.stderr)
    deployments = tmp_path / "state" / "deployments"
    generations = tuple(
        path for path in deployments.glob("*.json") if path.name != "current.json"
    )
    assert len(generations) == 20
    assert not any(path.name.startswith("20260701T") for path in generations)
    latest_generation = next(
        path
        for path in generations
        if json.loads(path.read_text(encoding="utf-8"))["targetCommit"] == NEXT_REVISION
    )
    current = deployments / "current.json"
    assert current.exists()
    assert json.loads(current.read_text(encoding="utf-8")) == json.loads(
        latest_generation.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "arguments",
    ((), ("--commit", "abc"), ("--commit", NEXT_REVISION, "--skip-backup")),
    ids=("missing-commit", "incomplete-sha", "backup-bypass"),
)
def test_should_reject_invalid_deploy_invocations_before_external_side_effects(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    bin_dir, call_log = _install_rejection_fakes(tmp_path)

    result = subprocess.run(
        [str(DOGFOOD_SCRIPTS_DIR / "deploy.sh"), *arguments],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DOGFOOD_ENV_FILE": str(env_path),
            "WSL_DISTRO_NAME": "Ubuntu-dogfood",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("script_name", "arguments"),
    ROOT_OPERATION_CASES,
    ids=("bootstrap", "deploy", "rollback"),
)
def test_should_handoff_noninteractive_root_operation_with_exit_code_three(
    tmp_path: Path,
    script_name: str,
    arguments: tuple[str, ...],
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    bin_dir = tmp_path / "handoff-bin"
    bin_dir.mkdir()
    sudo_log = tmp_path / "sudo.calls"
    write_executable(
        bin_dir / "id",
        'if [ "${1-}" = "-u" ]; then printf "1000\\n"; else exit 1; fi\n',
    )
    write_executable(
        bin_dir / "sudo",
        f'printf "%s\\n" "$*" >> {str(sudo_log)!r}\n[ "$*" = "-n true" ]\nexit 1\n',
    )

    result = subprocess.run(
        command_with_root_owned_revision(
            tmp_path / "config" / "dogfood.revision",
            [
                str(DOGFOOD_SCRIPTS_DIR / script_name),
                *arguments,
            ],
        ),
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DOGFOOD_ENV_FILE": str(env_path),
            "WSL_DISTRO_NAME": "Ubuntu-dogfood",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 3
    diagnostic = result.stdout + result.stderr
    assert "sudo env " in diagnostic
    assert f"DOGFOOD_ENV_FILE={env_path}" in diagnostic
    assert "WSL_DISTRO_NAME=Ubuntu-dogfood" in diagnostic
    assert str(DOGFOOD_SCRIPTS_DIR / script_name) in diagnostic
    if arguments:
        assert " ".join(arguments) in diagnostic
    assert TEST_SECRET_SENTINEL not in diagnostic
    assert env_path.is_file()
    if sudo_log.exists():
        assert sudo_log.read_text(encoding="utf-8").splitlines() == ["-n true"]


@pytest.mark.parametrize(
    ("script_name", "arguments"),
    ROOT_OPERATION_CASES,
    ids=("bootstrap", "deploy", "rollback"),
)
def test_should_reject_without_handoff_when_non_root_operation_is_interactive(
    tmp_path: Path,
    script_name: str,
    arguments: tuple[str, ...],
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    bin_dir = tmp_path / "interactive-root-bin"
    bin_dir.mkdir()
    side_effect_log = tmp_path / "post-root-guard.calls"
    write_executable(
        bin_dir / "id",
        'if [ "${1-}" = "-u" ]; then printf "1000\\n"; else exit 1; fi\n',
    )
    write_executable(
        bin_dir / "sudo",
        '[ "$*" = "-n true" ]\n',
    )
    for command in ROOT_GUARD_POST_COMMANDS:
        write_executable(
            bin_dir / command,
            f'printf "%s\\n" "{command}" >> {str(side_effect_log)!r}\nexit 99\n',
        )
    master_fd, slave_fd = os.openpty()
    try:
        result = subprocess.run(
            command_with_root_owned_revision(
                tmp_path / "config" / "dogfood.revision",
                [str(DOGFOOD_SCRIPTS_DIR / script_name), *arguments],
            ),
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "DOGFOOD_ENV_FILE": str(env_path),
                "WSL_DISTRO_NAME": "Ubuntu-dogfood",
            },
            stdin=slave_fd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        os.close(slave_fd)
        os.close(master_fd)

    assert result.returncode == 2
    diagnostic = result.stdout + result.stderr
    assert "dogfood配備操作はroot権限で実行してください" in diagnostic
    assert "sudo env " not in diagnostic
    assert not side_effect_log.exists()


@pytest.mark.parametrize(
    ("script_name", "arguments"),
    ROOT_OPERATION_CASES,
    ids=("bootstrap", "deploy", "rollback"),
)
def test_should_handoff_interactive_root_operation_when_sudo_probe_fails(
    tmp_path: Path,
    script_name: str,
    arguments: tuple[str, ...],
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    bin_dir = tmp_path / "interactive-handoff-bin"
    bin_dir.mkdir()
    sudo_log = tmp_path / "sudo.calls"
    side_effect_log = tmp_path / "post-root-guard.calls"
    write_executable(
        bin_dir / "id",
        'if [ "${1-}" = "-u" ]; then printf "1000\\n"; else exit 1; fi\n',
    )
    write_executable(
        bin_dir / "sudo",
        f'printf "%s\\n" "$*" >> {str(sudo_log)!r}\nexit 1\n',
    )
    for command in ROOT_GUARD_POST_COMMANDS:
        write_executable(
            bin_dir / command,
            f'printf "%s\\n" "{command}" >> {str(side_effect_log)!r}\nexit 99\n',
        )
    master_fd, slave_fd = os.openpty()
    try:
        result = subprocess.run(
            command_with_root_owned_revision(
                tmp_path / "config" / "dogfood.revision",
                [str(DOGFOOD_SCRIPTS_DIR / script_name), *arguments],
            ),
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "DOGFOOD_ENV_FILE": str(env_path),
                "WSL_DISTRO_NAME": "Ubuntu-dogfood",
            },
            stdin=slave_fd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        os.close(slave_fd)
        os.close(master_fd)

    assert result.returncode == 3
    assert sudo_log.read_text(encoding="utf-8").splitlines() == ["-n true"]
    diagnostic = result.stdout + result.stderr
    assert "sudo env " in diagnostic
    assert f"DOGFOOD_ENV_FILE={env_path}" in diagnostic
    assert "WSL_DISTRO_NAME=Ubuntu-dogfood" in diagnostic
    assert str(DOGFOOD_SCRIPTS_DIR / script_name) in diagnostic
    if arguments:
        assert " ".join(arguments) in diagnostic
    assert TEST_SECRET_SENTINEL not in diagnostic
    assert not side_effect_log.exists()


def test_should_leave_revision_and_checkout_unchanged_when_only_main_changes(
    tmp_path: Path,
) -> None:
    env_path, _ = write_dogfood_env(tmp_path)
    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    clone_dir = tmp_path / "clone"
    subprocess.run(
        ["git", "init", "--bare", str(origin)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(source)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (source / "version").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "version"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "-m", "first"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "origin", str(origin)], check=True
    )
    subprocess.run(
        ["git", "-C", str(source), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", "--branch", "main", str(origin), str(clone_dir)],
        check=True,
        capture_output=True,
    )
    before_checkout = subprocess.run(
        ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    revision_path = write_dogfood_revision(tmp_path, before_checkout.strip())
    before_revision = revision_path.read_bytes()

    (source / "version").write_text("two", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(source), "commit", "-am", "second"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "push", "origin", "main"],
        check=True,
        capture_output=True,
    )

    observed = subprocess.run(
        command_with_root_owned_revision(
            revision_path,
            [
                "bash",
                "-c",
                'source "$1"; dogfood_load_environment; '
                'printf "%s\\n" "$DOGFOOD_REPOSITORY_REVISION"; '
                'git -C "$DOGFOOD_CLONE_DIR" rev-parse HEAD',
                "bash",
                str(DOGFOOD_SCRIPTS_DIR / "load-environment.sh"),
            ],
        ),
        env={**os.environ, "DOGFOOD_ENV_FILE": str(env_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert observed.returncode == 0, (observed.stdout, observed.stderr)
    assert observed.stdout.splitlines() == [
        before_checkout.strip(),
        before_checkout.strip(),
    ]
    assert revision_path.read_bytes() == before_revision


def test_should_restore_full_history_before_checking_main_ancestry(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    clone_dir = tmp_path / "clone"
    subprocess.run(
        ["git", "init", "--bare", str(origin)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(source)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    for value in ("one", "two"):
        (source / "version").write_text(value, encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "version"], check=True)
        subprocess.run(
            ["git", "-C", str(source), "commit", "-m", value],
            check=True,
            capture_output=True,
        )
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "origin", str(origin)], check=True
    )
    subprocess.run(
        ["git", "-C", str(source), "push", "origin", "main"],
        check=True,
        capture_output=True,
    )
    origin_url = origin.as_uri()
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", "main", origin_url, str(clone_dir)],
        check=True,
        capture_output=True,
    )
    target = subprocess.run(
        ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (clone_dir / ".git" / "shallow").is_file()

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; DOGFOOD_CLONE_DIR=$2; DOGFOOD_REPOSITORY_URL=$3; '
            'dogfood_verify_origin; dogfood_fetch_and_resolve_commit "$4"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "deployment-lib.sh"),
            str(clone_dir),
            origin_url,
            target,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert not (clone_dir / ".git" / "shallow").exists()


def test_should_publish_revision_only_after_the_complete_sha_is_ready(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    revision = config_dir / "dogfood.revision"
    revision.write_text(f"{TEST_REVISION}\n", encoding="utf-8")
    revision.chmod(0o640)
    bin_dir = tmp_path / "atomic-bin"
    bin_dir.mkdir()
    install_ready = tmp_path / "install.ready"
    install_release = tmp_path / "install.release"
    write_executable(
        bin_dir / "install",
        'arguments=()\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in -o|-g) shift 2 ;; *) arguments+=("$1"); shift ;; esac\n'
        'done\n'
        'source_path="${arguments[${#arguments[@]}-2]}"\n'
        'destination="${arguments[${#arguments[@]}-1]}"\n'
        'case "$destination" in\n'
        '  */dogfood.revision) head -c 5 "$source_path" > "$destination" ;;\n'
        '  *) /usr/bin/install "${arguments[@]}" ;;\n'
        'esac\n'
        'touch "$ATOMIC_INSTALL_READY"\n'
        'while [ ! -e "$ATOMIC_INSTALL_RELEASE" ]; do :; done\n'
        'case "$destination" in */dogfood.revision) tail -c +6 "$source_path" >> "$destination" ;; esac\n',
    )
    process = subprocess.Popen(
        [
            "bash",
            "-c",
            'source "$1"; DOGFOOD_CONFIG_DIR=$2; DOGFOOD_SERVICE_GROUP=$3; dogfood_update_revision "$4"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "deployment-lib.sh"),
            str(config_dir),
            TEST_SERVICE_GROUP,
            NEXT_REVISION,
        ],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ATOMIC_INSTALL_READY": str(install_ready),
            "ATOMIC_INSTALL_RELEASE": str(install_release),
        },
    )
    try:
        deadline = time.monotonic() + 10
        while not install_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert install_ready.exists()
        observed = {revision.read_text(encoding="utf-8")}
        install_release.touch()
        assert process.wait(timeout=10) == 0
        observed.add(revision.read_text(encoding="utf-8"))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    assert observed <= {f"{TEST_REVISION}\n", f"{NEXT_REVISION}\n"}
    assert f"{NEXT_REVISION}\n" in observed


def test_should_keep_distinct_manifests_for_repeated_same_revision_operations(
    tmp_path: Path,
) -> None:
    deployments = tmp_path / "state" / "deployments"
    deployments.mkdir(parents=True)
    (tmp_path / "state").chmod(0o750)
    deployments.chmod(0o750)
    bin_dir = tmp_path / "manifest-bin"
    bin_dir.mkdir()
    write_executable(
        bin_dir / "install",
        'arguments=()\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in -o|-g) shift 2 ;; *) arguments+=("$1"); shift ;; esac\n'
        'done\n'
        'exec /usr/bin/install "${arguments[@]}"\n',
    )
    manifest = json.dumps(
        {
            "previousCommit": TEST_REVISION,
            "targetCommit": NEXT_REVISION,
            "profileSchemaVersion": 1,
            "dataSchemaVersion": 3,
            "backupId": "/backups/repeated",
            "deployedAt": "2026-08-14T00:00:00Z",
        },
        separators=(",", ":"),
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; DOGFOOD_STATE_DIR=$2; DOGFOOD_SERVICE_GROUP=$3; '
            'dogfood_write_manifest "$4" "$5"; dogfood_write_manifest "$4" "$5"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "deployment-lib.sh"),
            str(tmp_path / "state"),
            TEST_SERVICE_GROUP,
            manifest,
            NEXT_REVISION,
        ],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    generations = tuple(
        path for path in deployments.glob("*.json") if path.name != "current.json"
    )
    assert len(generations) == 2
    assert len({path.name for path in generations}) == 2
    assert json.loads((deployments / "current.json").read_text(encoding="utf-8")) == json.loads(manifest)


@pytest.mark.parametrize("failed_install", (1, 2), ids=("generation", "current"))
def test_should_remove_manifest_temporaries_when_install_fails(
    tmp_path: Path,
    failed_install: int,
) -> None:
    deployments = tmp_path / "state" / "deployments"
    deployments.mkdir(parents=True)
    (tmp_path / "state").chmod(0o750)
    deployments.chmod(0o750)
    bin_dir = tmp_path / "manifest-failure-bin"
    bin_dir.mkdir()
    install_count = tmp_path / "install-count"
    write_executable(
        bin_dir / "install",
        'count=$(cat "$MANIFEST_INSTALL_COUNT" 2>/dev/null || printf 0)\n'
        'count=$((count + 1)); printf "%s" "$count" > "$MANIFEST_INSTALL_COUNT"\n'
        'arguments=()\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in -o|-g) shift 2 ;; *) arguments+=("$1"); shift ;; esac\n'
        'done\n'
        'destination="${arguments[${#arguments[@]}-1]}"\n'
        'if [ "$count" -eq "$MANIFEST_FAILED_INSTALL" ]; then\n'
        '  : > "$destination"\n'
        '  exit 1\n'
        'fi\n'
        'exec /usr/bin/install "${arguments[@]}"\n',
    )
    manifest = json.dumps(
        {
            "previousCommit": TEST_REVISION,
            "targetCommit": NEXT_REVISION,
            "profileSchemaVersion": 1,
            "dataSchemaVersion": 3,
            "backupId": "/backups/failure",
            "deployedAt": "2026-08-14T00:00:00Z",
        },
        separators=(",", ":"),
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; DOGFOOD_STATE_DIR=$2; DOGFOOD_SERVICE_GROUP=$3; '
            'dogfood_write_manifest "$4" "$5"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "deployment-lib.sh"),
            str(tmp_path / "state"),
            TEST_SERVICE_GROUP,
            manifest,
            NEXT_REVISION,
        ],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "MANIFEST_INSTALL_COUNT": str(install_count),
            "MANIFEST_FAILED_INSTALL": str(failed_install),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert not tuple(deployments.glob(".*"))


def test_should_stop_manifest_generation_after_finite_link_attempts(
    tmp_path: Path,
) -> None:
    deployments = tmp_path / "state" / "deployments"
    deployments.mkdir(parents=True)
    (tmp_path / "state").chmod(0o750)
    deployments.chmod(0o750)
    bin_dir = tmp_path / "manifest-link-bin"
    bin_dir.mkdir()
    link_log = tmp_path / "link-attempts"
    write_executable(
        bin_dir / "install",
        'arguments=()\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in -o|-g) shift 2 ;; *) arguments+=("$1"); shift ;; esac\n'
        'done\n'
        'exec /usr/bin/install "${arguments[@]}"\n',
    )
    write_executable(
        bin_dir / "ln",
        'printf "attempt\\n" >> "$MANIFEST_LINK_LOG"\nexit 1\n',
    )
    manifest = json.dumps(
        {
            "previousCommit": TEST_REVISION,
            "targetCommit": NEXT_REVISION,
            "profileSchemaVersion": 1,
            "dataSchemaVersion": 3,
            "backupId": "/backups/link-failure",
            "deployedAt": "2026-08-14T00:00:00Z",
        },
        separators=(",", ":"),
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; DOGFOOD_STATE_DIR=$2; DOGFOOD_SERVICE_GROUP=$3; '
            'dogfood_write_manifest "$4" "$5"',
            "bash",
            str(DOGFOOD_SCRIPTS_DIR / "deployment-lib.sh"),
            str(tmp_path / "state"),
            TEST_SERVICE_GROUP,
            manifest,
            NEXT_REVISION,
        ],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "MANIFEST_LINK_LOG": str(link_log),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert link_log.read_text(encoding="utf-8").splitlines() == ["attempt"] * 16
    assert not tuple(deployments.glob("*.json"))
    assert not tuple(deployments.glob(".*"))

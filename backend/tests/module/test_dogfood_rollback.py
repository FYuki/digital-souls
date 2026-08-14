from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tests.dogfood_infrastructure_test_support import (
    DOGFOOD_SCRIPTS_DIR,
    TEST_REVISION,
    TEST_SERVICE_GROUP,
    command_with_root_owned_revision,
    read_valid_deployment_manifest,
    write_dogfood_env,
    write_executable,
)


OLDER_REVISION = "fedcba9876543210fedcba9876543210fedcba98"


def _rollback_command(tmp_path: Path, arguments: tuple[str, ...] = ()) -> list[str]:
    return command_with_root_owned_revision(
        tmp_path / "config" / "dogfood.revision",
        [str(DOGFOOD_SCRIPTS_DIR / "rollback.sh"), *arguments],
    )


def _write_manifest(path: Path, previous: str, target: str) -> None:
    path.write_text(
        json.dumps(
            {
                "previousCommit": previous,
                "targetCommit": target,
                "profileSchemaVersion": 1,
                "dataSchemaVersion": 0,
                "backupId": "backup-test-generation",
                "deployedAt": "2026-08-14T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o640)


def _rollback_environment(
    tmp_path: Path, *, failure: str | None = None
) -> tuple[dict[str, str], Path]:
    env_path, data_dir = write_dogfood_env(tmp_path)
    deployments = tmp_path / "state" / "deployments"
    deployments.mkdir(parents=True)
    (tmp_path / "state").chmod(0o750)
    deployments.chmod(0o750)
    current = deployments / f"20260814T000000Z-{TEST_REVISION[:12]}.json"
    older = deployments / f"20260813T000000Z-{OLDER_REVISION[:12]}.json"
    _write_manifest(current, OLDER_REVISION, TEST_REVISION)
    _write_manifest(older, "0" * 40, OLDER_REVISION)
    (deployments / "current.json").write_text(
        current.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (deployments / "current.json").chmod(0o640)
    call_log = tmp_path / "rollback.calls"
    head_path = tmp_path / "head"
    head_path.write_text(TEST_REVISION, encoding="utf-8")
    clone_dir = tmp_path / "clone"
    (clone_dir / ".git").mkdir(parents=True)
    profile = clone_dir / "environments" / "profiles" / "dogfood.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"schemaVersion": 1}), encoding="utf-8")
    database = data_dir / "conversation-history.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 3")
    setup_backend = clone_dir / "scripts" / "setup-backend.sh"
    setup_backend.parent.mkdir(parents=True)
    write_executable(
        setup_backend,
        f'printf "backend-setup\\n" >> "{call_log}"\n'
        '[ "${ROLLBACK_FAILURE-}" != "backend-setup" ]\n',
    )
    restart = clone_dir / "scripts" / "dogfood" / "restart-services.sh"
    restart.parent.mkdir(parents=True)
    write_executable(
        restart,
        f'printf "restart\\n" >> "{call_log}"\n'
        '[ "${ROLLBACK_FAILURE-}" != "restart" ]\n',
    )
    readiness = clone_dir / "environments" / "environment_cli.py"
    readiness.parent.mkdir(parents=True, exist_ok=True)
    write_executable(
        readiness,
        f'printf "cli\\t%s\\n" "$*" >> "{call_log}"\n[ "$1" = "readiness" ]\n',
    )
    python = clone_dir / "backend" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    write_executable(python, 'exec "$@"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "id", 'printf "0\\n"\n')
    write_executable(
        bin_dir / "git",
        f'printf "git\\t%s\\n" "$*" >> "{call_log}"\n'
        'case "$*" in\n'
        '  *"remote get-url origin"*) printf "%s\\n" "$DOGFOOD_REPOSITORY_URL" ;;\n'
        '  *"status --porcelain"*) true ;;\n'
        '  *"merge-base --is-ancestor"*) true ;;\n'
        f'  *"rev-parse HEAD"*) cat "{head_path}" ;;\n'
        '  *"rev-parse --verify"*) printf "%s\\n" "$ROLLBACK_TARGET" ;;\n'
        '  *"symbolic-ref --quiet HEAD"*) exit 1 ;;\n'
        f'  *"checkout --detach"*) printf "%s" "${{@: -1}}" > "{head_path}" ;;\n'
        "esac\n",
    )
    write_executable(
        bin_dir / "sudo",
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in --preserve-env=*) shift ;; -u) shift 2 ;; *) break ;; esac\n'
        'done\nexec "$@"\n',
    )
    write_executable(
        bin_dir / "install",
        f'printf "install\\t%s\\n" "$*" >> "{call_log}"\n'
        'arguments=()\nwhile [ "$#" -gt 0 ]; do\n'
        '  case "$1" in -o|-g) shift 2 ;; *) arguments+=("$1"); shift ;; esac\n'
        'done\nexec /usr/bin/install "${arguments[@]}"\n',
    )
    for command in ("npm", "systemctl", "chown", "chmod"):
        write_executable(
            bin_dir / command,
            f'printf "%s\\t%s\\n" "{command}" "$*" >> "{call_log}"\n'
            f'[ "${{ROLLBACK_FAILURE-}}" != "{command}" ]\n',
        )
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DOGFOOD_ENV_FILE": str(env_path),
        "WSL_DISTRO_NAME": "Ubuntu-dogfood",
        "ROLLBACK_TARGET": OLDER_REVISION,
    }
    if failure is not None:
        environment["ROLLBACK_FAILURE"] = failure
    return environment, call_log


@pytest.mark.parametrize(
    ("arguments", "expected_revision"),
    (((), OLDER_REVISION), (("--to", OLDER_REVISION), OLDER_REVISION)),
    ids=("previous-generation", "saved-generation"),
)
def test_should_select_only_a_saved_manifest_for_rollback(
    tmp_path: Path,
    arguments: tuple[str, ...],
    expected_revision: str,
) -> None:
    environment, call_log = _rollback_environment(tmp_path)

    result = subprocess.run(
        _rollback_command(tmp_path, arguments),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{expected_revision}\n"
    calls = tuple(call_log.read_text(encoding="utf-8").splitlines())
    operation_positions = tuple(
        next(index for index, call in enumerate(calls) if marker in call)
        for marker in (
            "checkout --detach",
            "backend-setup",
            "npm\t",
            "chown\t",
            "chmod\t",
            "restart",
            "cli\treadiness ",
        )
    )
    assert operation_positions == tuple(sorted(operation_positions))


def test_should_use_current_manifest_for_first_manual_rollback(
    tmp_path: Path,
) -> None:
    environment, _ = _rollback_environment(tmp_path)
    deployments = tmp_path / "state" / "deployments"
    for path in deployments.glob(f"*-{OLDER_REVISION[:12]}.json"):
        path.unlink()

    result = subprocess.run(
        _rollback_command(tmp_path),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{OLDER_REVISION}\n"


def test_should_record_a_new_current_manifest_for_rollback(tmp_path: Path) -> None:
    environment, call_log = _rollback_environment(tmp_path)
    deployments = tmp_path / "state" / "deployments"
    current = deployments / "current.json"
    previous_current = current.read_text(encoding="utf-8")
    generations_before = {
        path for path in deployments.glob("*.json") if path.name != current.name
    }

    result = subprocess.run(
        _rollback_command(tmp_path),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    generations_after = {
        path for path in deployments.glob("*.json") if path.name != current.name
    }
    created_generations = generations_after - generations_before
    assert len(created_generations) == 1
    generation = created_generations.pop()
    manifest = read_valid_deployment_manifest(
        generation,
        {
            "previousCommit": TEST_REVISION,
            "targetCommit": OLDER_REVISION,
            "profileSchemaVersion": 1,
            "dataSchemaVersion": 3,
            "backupId": "backup-test-generation",
        },
    )
    calls = tuple(call_log.read_text(encoding="utf-8").splitlines())
    assert any(
        "install\t" in call
        and "-m 0640" in call
        and "-o root" in call
        and f"-g {TEST_SERVICE_GROUP}" in call
        and "/.manifest.ready." in call
        for call in calls
    )
    assert current.read_text(encoding="utf-8") != previous_current
    assert json.loads(current.read_text(encoding="utf-8")) == manifest


def test_should_reject_a_revision_without_a_saved_manifest(tmp_path: Path) -> None:
    environment, call_log = _rollback_environment(tmp_path)
    unsaved_revision = "1" * 40

    result = subprocess.run(
        _rollback_command(tmp_path, ("--to", unsaved_revision)),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert not call_log.exists()
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{TEST_REVISION}\n"


@pytest.mark.parametrize(
    ("failure", "last_operation"),
    (
        ("backend-setup", "backend-setup"),
        ("npm", "npm\t"),
        ("chown", "chown\t"),
        ("chmod", "chmod\t"),
        ("restart", "restart"),
    ),
)
def test_should_stop_rollback_at_the_first_activation_failure(
    tmp_path: Path,
    failure: str,
    last_operation: str,
) -> None:
    environment, call_log = _rollback_environment(tmp_path, failure=failure)

    result = subprocess.run(
        _rollback_command(tmp_path),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    calls = tuple(call_log.read_text(encoding="utf-8").splitlines())
    assert last_operation in calls[-1]
    assert not any(call.startswith("cli\treadiness ") for call in calls)


def test_should_reject_a_symlinked_manifest_before_rollback_side_effects(
    tmp_path: Path,
) -> None:
    environment, call_log = _rollback_environment(tmp_path)
    current = tmp_path / "state" / "deployments" / "current.json"
    outside = tmp_path / "outside.json"
    outside.write_text(current.read_text(encoding="utf-8"), encoding="utf-8")
    current.unlink()
    current.symlink_to(outside)

    result = subprocess.run(
        _rollback_command(tmp_path),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert not call_log.exists()


@pytest.mark.parametrize("unsafe_parent", ("symlink", "non-root-owner"))
def test_should_reject_an_unsafe_manifest_parent_before_rollback_side_effects(
    tmp_path: Path,
    unsafe_parent: str,
) -> None:
    environment, call_log = _rollback_environment(tmp_path)
    env_path = Path(environment["DOGFOOD_ENV_FILE"])
    manifest_root = tmp_path / "manifest-root"
    manifest_root.mkdir()
    intermediate = manifest_root / "intermediate"
    command = [str(DOGFOOD_SCRIPTS_DIR / "rollback.sh")]
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

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Literal

import pytest

from tests.dogfood_infrastructure_test_support import (
    install_bootstrap_command_fakes,
    prepare_bootstrap_clone,
    prepare_initial_bootstrap_clone_assets,
    prepare_target_bootstrap_clone_assets,
    write_dogfood_env,
)


ROOT_DIR = Path(__file__).parent.parent.parent.parent
DOGFOOD_SCRIPTS_DIR = ROOT_DIR / "scripts" / "dogfood"
LIFECYCLE_CASES = (
    ("start-services.sh", "start"),
    ("stop-services.sh", "stop"),
    ("restart-services.sh", "restart"),
)
BOOTSTRAP_FAILURES = ("fetch", "revision", "branch", "dirty", "origin")
VALID_BACKUP_OUTPUT_TEMPLATE = (
    '{{"status":"ok","backupDirectory":"{backup_root}/backup-test-generation"}}'
)


def _install_systemctl_fake(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "systemctl.calls"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_CALL_LOG"\n',
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    return fake_bin, call_log


def _run_lifecycle_script(
    tmp_path: Path,
    script_name: str,
    *,
    environment_id: str,
    wsl_distribution: str,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    env_path, _ = write_dogfood_env(tmp_path)
    source = env_path.read_text(encoding="utf-8").replace(
        "DS_ENVIRONMENT_ID=dogfood",
        f"DS_ENVIRONMENT_ID={environment_id}",
    )
    env_path.write_text(source, encoding="utf-8")
    fake_bin, call_log = _install_systemctl_fake(tmp_path)
    environment = {
        **os.environ,
        "DOGFOOD_ENV_FILE": str(env_path),
        "WSL_DISTRO_NAME": wsl_distribution,
        "SYSTEMCTL_CALL_LOG": str(call_log),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    result = subprocess.run(
        [str(DOGFOOD_SCRIPTS_DIR / script_name)],
        env=environment,
        capture_output=True,
        text=True,
    )
    return result, call_log


@pytest.mark.parametrize(("script_name", "action"), LIFECYCLE_CASES)
def test_should_reach_systemctl_when_dogfood_identity_matches(
    tmp_path: Path,
    script_name: str,
    action: str,
) -> None:
    result, call_log = _run_lifecycle_script(
        tmp_path,
        script_name,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode == 0, result.stderr
    assert call_log.read_text(encoding="utf-8") == (
        f"{action} digital-souls-inference.target\n"
    )


@pytest.mark.parametrize(("script_name", "_action"), LIFECYCLE_CASES)
@pytest.mark.parametrize(
    ("environment_id", "wsl_distribution"),
    (("development", "Ubuntu-dogfood"), ("dogfood", "Ubuntu-dev")),
)
def test_should_stop_before_systemctl_when_dogfood_identity_does_not_match(
    tmp_path: Path,
    script_name: str,
    _action: str,
    environment_id: str,
    wsl_distribution: str,
) -> None:
    result, call_log = _run_lifecycle_script(
        tmp_path,
        script_name,
        environment_id=environment_id,
        wsl_distribution=wsl_distribution,
    )

    assert result.returncode != 0
    assert not call_log.exists()


def _run_bootstrap(
    tmp_path: Path,
    failure: str | None,
    docker_member: bool,
    clone_scenario: Literal["existing", "initial"],
    *,
    database_exists: bool,
    environment_id: str,
    wsl_distribution: str,
    backup_output: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], tuple[str, ...]]:
    env_path, _ = write_dogfood_env(tmp_path)
    if database_exists:
        (tmp_path / "data" / "conversation-history.db").write_bytes(
            b"existing database"
        )
    source = env_path.read_text(encoding="utf-8").replace(
        "DS_ENVIRONMENT_ID=dogfood",
        f"DS_ENVIRONMENT_ID={environment_id}",
    )
    env_path.write_text(source, encoding="utf-8")
    initial_clone_assets: Path | None = None
    target_clone_assets: Path | None = None
    if clone_scenario == "existing":
        prepare_bootstrap_clone(tmp_path)
    else:
        initial_clone_assets = prepare_initial_bootstrap_clone_assets(tmp_path)
    if database_exists:
        target_clone_assets = prepare_target_bootstrap_clone_assets(tmp_path)
    fake_bin, call_log = install_bootstrap_command_fakes(tmp_path)
    environment = {
        **os.environ,
        "DOGFOOD_ENV_FILE": str(env_path),
        "WSL_DISTRO_NAME": wsl_distribution,
        "BOOTSTRAP_CALL_LOG": str(call_log),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    environment["BOOTSTRAP_BACKUP_OUTPUT"] = (
        VALID_BACKUP_OUTPUT_TEMPLATE.format(backup_root=tmp_path / "backups")
        if backup_output is None
        else backup_output
    )
    if failure is not None:
        environment["BOOTSTRAP_FAILURE"] = failure
    if docker_member:
        environment["BOOTSTRAP_DOCKER_MEMBER"] = "1"
    if initial_clone_assets is not None:
        environment["BOOTSTRAP_INITIAL_CLONE_ASSETS"] = str(initial_clone_assets)
    if target_clone_assets is not None:
        environment["BOOTSTRAP_TARGET_CLONE_ASSETS"] = str(target_clone_assets)
    result = subprocess.run(
        [str(DOGFOOD_SCRIPTS_DIR / "bootstrap.sh")],
        env=environment,
        capture_output=True,
        text=True,
    )
    calls = tuple(call_log.read_text(encoding="utf-8").splitlines())
    return result, calls


def _post_gate_side_effect_calls(
    calls: tuple[str, ...], temporary_paths: tuple[Path, ...]
) -> tuple[str, ...]:
    def targets_temporary_path(call: str) -> bool:
        arguments = shlex.split(call.partition("\t")[2])
        if not arguments:
            return False
        target = Path(arguments[-1])
        return any(target.is_relative_to(path) for path in temporary_paths)

    return tuple(
        call
        for call in calls
        if call == "renderer"
        or (
            call.startswith(("chown\t", "chmod\t"))
            and not targets_temporary_path(call)
        )
        or call.startswith("systemctl\t")
        or (
            call.startswith("install\t")
            and not call.endswith("/backups")
        )
    )


def _git_fetch_calls(calls: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        call
        for call in calls
        if call.startswith("git\t") and " fetch --depth 1 origin " in call
    )


def _main_clone_fetch_calls(
    calls: tuple[str, ...], clone_dir: Path
) -> tuple[str, ...]:
    prefix = f"git\t-C {clone_dir} fetch --depth 1 origin "
    return tuple(call for call in calls if call.startswith(prefix))


def _git_origin_lookup_calls(calls: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        call
        for call in calls
        if call.startswith("git\t") and " remote get-url origin" in call
    )


def _backup_calls(calls: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        call for call in calls if call.startswith("sudo\t") and " backup " in call
    )


def _target_cli_calls(calls: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(call for call in calls if call.startswith("target-cli\t"))


def _target_backup_calls(calls: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(call for call in calls if call.startswith("target-cli\tbackup "))


def _target_backup_verify_calls(calls: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        call for call in calls if call.startswith("target-cli\tbackup-verify ")
    )


def _backup_clone_paths(calls: tuple[str, ...], main_clone: Path) -> tuple[Path, ...]:
    clone_paths = (
        Path(shlex.split(call.partition("\t")[2])[-1])
        for call in calls
        if call.startswith("git\tclone --no-checkout ")
    )
    return tuple(path for path in clone_paths if path != main_clone)


def test_should_place_assets_only_after_bootstrap_trust_checks_succeed(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        True,
        "existing",
        database_exists=False,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode == 0, result.stderr
    assert "renderer" in calls
    assert _backup_calls(calls) == ()
    assert _target_cli_calls(calls) == ()
    assert "gpasswd\t--delete digital-souls docker" in calls
    assert any(call.startswith("install\t") for call in calls)
    assert "systemctl\tdaemon-reload" in calls
    assert "systemctl\tenable digital-souls-inference.target" in calls


@pytest.mark.parametrize("failure", BOOTSTRAP_FAILURES)
def test_should_stop_before_asset_placement_when_bootstrap_trust_check_fails(
    tmp_path: Path,
    failure: str,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        failure,
        False,
        "existing",
        database_exists=False,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode != 0
    assert _post_gate_side_effect_calls(calls, ()) == ()


def test_should_stop_before_fetch_when_bootstrap_origin_does_not_match(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        "origin",
        False,
        "existing",
        database_exists=False,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode != 0
    assert len(_git_origin_lookup_calls(calls)) == 1
    assert _git_fetch_calls(calls) == ()


def test_should_use_target_revision_cli_before_fetching_existing_clone(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        database_exists=True,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )
    backup_install = (
        "install\t-d -m 0750 -o digital-souls -g digital-souls "
        f"{tmp_path / 'backups'}"
    )
    main_fetch_calls = _main_clone_fetch_calls(calls, tmp_path / "clone")
    backup_clones = _backup_clone_paths(calls, tmp_path / "clone")

    assert result.returncode == 0, result.stderr
    assert calls.count(backup_install) == 1
    assert (tmp_path / "backups").stat().st_mode & 0o777 == 0o750
    assert len(backup_clones) == 1
    backup_clone = backup_clones[0]
    backup_clone_chown = f"chown\troot:digital-souls {backup_clone}"
    backup_clone_chmod = f"chmod\t2750 {backup_clone}"
    assert len(_backup_calls(calls)) == 1
    assert "--preserve-env=DOGFOOD_BACKUP_AUTHENTICATION_KEY" in _backup_calls(calls)[0]
    exposed_output = "\n".join((*calls, result.stdout, result.stderr))
    assert ("ab" * 32) not in exposed_output
    assert len(_target_backup_calls(calls)) == 1
    assert len(_target_backup_verify_calls(calls)) == 1
    assert not (tmp_path / "clone" / "backend" / ".venv").exists()
    assert len(main_fetch_calls) == 1
    target_operations = (
        f"git\t-C {backup_clone} fetch --depth 1 origin ",
        f"git\t-C {backup_clone} rev-parse --verify ",
        f"git\t-c core.hooksPath=/dev/null -C {backup_clone} checkout --detach ",
        f"git\t-C {backup_clone} rev-parse HEAD",
    )
    target_operation_indices = tuple(
        next(
            index
            for index, call in enumerate(calls)
            if call.startswith(operation)
        )
        for operation in target_operations
    )
    target_cli_index = calls.index(_target_backup_calls(calls)[0])
    main_clone_calls_before_backup = tuple(
        call
        for call in calls[:target_cli_index]
        if call.startswith(f"git\t-C {tmp_path / 'clone'} ")
    )
    assert target_operation_indices == tuple(sorted(target_operation_indices))
    assert target_operation_indices[-1] < target_cli_index
    assert main_clone_calls_before_backup == _git_origin_lookup_calls(calls)
    target_cli_path = backup_clone / "environments" / "environment_cli.py"
    assert str(target_cli_path) in _backup_calls(calls)[0]
    assert f"--repository-root {backup_clone}" in _backup_calls(calls)[0]
    assert calls.index(_git_origin_lookup_calls(calls)[0]) < calls.index(backup_install)
    assert calls.index(backup_install) < calls.index(_target_cli_calls(calls)[0])
    assert calls.index(backup_clone_chown) < calls.index(_target_cli_calls(calls)[0])
    assert calls.index(backup_clone_chmod) < calls.index(_target_cli_calls(calls)[0])
    backup_call_index = calls.index(_target_backup_calls(calls)[0])
    verify_call_index = calls.index(_target_backup_verify_calls(calls)[0])
    assert backup_call_index < verify_call_index < calls.index(main_fetch_calls[0])
    assert _target_backup_verify_calls(calls)[0].endswith(
        f"--backup-directory {tmp_path / 'backups' / 'backup-test-generation'}"
    )
    assert not backup_clone.exists()


def test_should_verify_backup_before_fetching_initial_clone_with_existing_database(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "initial",
        database_exists=True,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )
    main_clone = tmp_path / "clone"
    backup_clones = _backup_clone_paths(calls, main_clone)

    assert result.returncode == 0, result.stderr
    assert len(backup_clones) == 1
    backup_clone = backup_clones[0]
    assert "backend-setup" in calls
    assert len(_target_backup_calls(calls)) == 1
    assert len(_target_backup_verify_calls(calls)) == 1
    backup_call = _backup_calls(calls)[0]
    assert str(backup_clone / "backend" / ".venv" / "bin" / "python") in backup_call
    main_fetch = _main_clone_fetch_calls(calls, main_clone)
    assert len(main_fetch) == 1
    assert (
        calls.index(_target_backup_calls(calls)[0])
        < calls.index(_target_backup_verify_calls(calls)[0])
        < calls.index(main_fetch[0])
    )
    assert not backup_clone.exists()


@pytest.mark.parametrize("clone_scenario", ("existing", "initial"))
@pytest.mark.parametrize(
    "backup_output",
    (
        "not-json",
        '{"status":"ok"}',
        '{"status":"ok","backupDirectory":"/backup","extra":true}',
        '{"status":"failed","backupDirectory":"/backup"}',
        '{"status":"ok","backupDirectory":1}',
        '{"status":"ok","backupDirectory":""}',
    ),
)
def test_should_stop_before_verification_when_backup_output_violates_contract(
    tmp_path: Path,
    clone_scenario: Literal["existing", "initial"],
    backup_output: str,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        clone_scenario,
        database_exists=True,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        backup_output=backup_output,
    )
    main_clone = tmp_path / "clone"
    backup_clones = _backup_clone_paths(calls, main_clone)

    assert result.returncode != 0
    assert len(_target_backup_calls(calls)) == 1
    assert _target_backup_verify_calls(calls) == ()
    assert _main_clone_fetch_calls(calls, main_clone) == ()
    assert len(backup_clones) == 1
    assert _post_gate_side_effect_calls(calls, backup_clones) == ()
    assert not backup_clones[0].exists()


@pytest.mark.parametrize("clone_scenario", ("existing", "initial"))
def test_deploy_gate_01_stops_before_main_fetch_when_backup_fails(
    tmp_path: Path,
    clone_scenario: Literal["existing", "initial"],
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        "backup",
        False,
        clone_scenario,
        database_exists=True,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert len(_git_origin_lookup_calls(calls)) == (1 if clone_scenario == "existing" else 0)
    assert len(_backup_calls(calls)) == 1
    assert len(_target_cli_calls(calls)) == 1
    assert result.returncode != 0
    assert _main_clone_fetch_calls(calls, tmp_path / "clone") == ()
    backup_clones = _backup_clone_paths(calls, tmp_path / "clone")
    assert len(backup_clones) == 1
    assert _post_gate_side_effect_calls(calls, backup_clones) == ()
    assert not backup_clones[0].exists()


@pytest.mark.parametrize("clone_scenario", ("existing", "initial"))
def test_should_stop_before_main_fetch_when_backup_verification_fails(
    tmp_path: Path,
    clone_scenario: Literal["existing", "initial"],
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        "verify",
        False,
        clone_scenario,
        database_exists=True,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    backup_clones = _backup_clone_paths(calls, tmp_path / "clone")

    assert result.returncode != 0
    assert len(_target_backup_calls(calls)) == 1
    assert len(_target_backup_verify_calls(calls)) == 1
    assert _main_clone_fetch_calls(calls, tmp_path / "clone") == ()
    assert len(backup_clones) == 1
    assert _post_gate_side_effect_calls(calls, backup_clones) == ()
    assert not backup_clones[0].exists()


def test_should_stop_before_backup_when_target_revision_does_not_match(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        "target-revision",
        False,
        "existing",
        database_exists=True,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode != 0
    assert _backup_calls(calls) == ()
    assert _target_cli_calls(calls) == ()
    assert _main_clone_fetch_calls(calls, tmp_path / "clone") == ()
    backup_clones = _backup_clone_paths(calls, tmp_path / "clone")
    assert len(backup_clones) == 1
    assert _post_gate_side_effect_calls(calls, backup_clones) == ()
    assert not backup_clones[0].exists()


@pytest.mark.parametrize("clone_scenario", ("existing", "initial"))
def test_should_stop_before_backup_when_target_runtime_setup_fails(
    tmp_path: Path,
    clone_scenario: Literal["existing", "initial"],
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        "backend-setup",
        False,
        clone_scenario,
        database_exists=True,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    backup_clones = _backup_clone_paths(calls, tmp_path / "clone")
    assert result.returncode != 0
    assert _backup_calls(calls) == ()
    assert _target_cli_calls(calls) == ()
    assert _main_clone_fetch_calls(calls, tmp_path / "clone") == ()
    assert len(backup_clones) == 1
    assert _post_gate_side_effect_calls(calls, backup_clones) == ()
    assert not backup_clones[0].exists()


def test_should_place_assets_after_initial_clone_trust_checks_succeed(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "initial",
        database_exists=False,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )
    required_git_operations = (
        "clone --no-checkout",
        "fetch --depth 1",
        "rev-parse --verify",
        "checkout --detach",
        "rev-parse HEAD",
        "symbolic-ref --quiet HEAD",
        "status --porcelain",
    )

    assert result.returncode == 0, result.stderr
    for operation in required_git_operations:
        assert any(call.startswith("git\t") and operation in call for call in calls)
    assert "renderer" in calls
    assert _backup_calls(calls) == ()
    assert _target_cli_calls(calls) == ()
    dirty_check_index = next(
        index
        for index, call in enumerate(calls)
        if call.startswith("git\t") and "status --porcelain" in call
    )
    assert calls.index("renderer") > dirty_check_index
    assert any(call.startswith("install\t") for call in calls)
    assert "systemctl\tdaemon-reload" in calls
    assert "systemctl\tenable digital-souls-inference.target" in calls


def test_should_stop_before_asset_placement_when_initial_clone_fails(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        "clone",
        False,
        "initial",
        database_exists=False,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode != 0
    assert any(
        call.startswith("git\tclone --no-checkout ")
        for call in calls
    )
    assert _post_gate_side_effect_calls(calls, ()) == ()


@pytest.mark.parametrize(
    ("environment_id", "wsl_distribution"),
    (("development", "Ubuntu-dogfood"), ("dogfood", "Ubuntu-dev")),
)
def test_should_stop_bootstrap_before_changes_when_identity_does_not_match(
    tmp_path: Path,
    environment_id: str,
    wsl_distribution: str,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "initial",
        database_exists=False,
        environment_id=environment_id,
        wsl_distribution=wsl_distribution,
    )

    assert result.returncode != 0
    assert calls == ()
    for path in ("clone", "config", "state", "log"):
        assert not (tmp_path / path).exists()

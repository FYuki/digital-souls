from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Literal

import pytest

from tests.dogfood_infrastructure_test_support import (
    TEST_SERVICE_GROUP,
    command_with_root_owned_revision,
    install_bootstrap_command_fakes,
    prepare_bootstrap_clone,
    prepare_initial_bootstrap_clone_assets,
    write_dogfood_env,
)


ROOT_DIR = Path(__file__).parent.parent.parent.parent
DOGFOOD_SCRIPTS_DIR = ROOT_DIR / "scripts" / "dogfood"
LIFECYCLE_CASES = (
    ("start-services.sh", "start"),
    ("stop-services.sh", "stop"),
    ("restart-services.sh", "restart"),
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
    revision_path = tmp_path / "config" / "dogfood.revision"
    result = subprocess.run(
        command_with_root_owned_revision(
            revision_path, [str(DOGFOOD_SCRIPTS_DIR / script_name)]
        ),
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
    environment_id: str,
    wsl_distribution: str,
    env_mode: int = 0o600,
    state_symlink_target: Path | None = None,
    state_path: Path | None = None,
    non_root_parent: Path | None = None,
    service_group_missing: bool = False,
) -> tuple[subprocess.CompletedProcess[str], tuple[str, ...]]:
    env_path, _ = write_dogfood_env(tmp_path)
    if state_symlink_target is not None:
        (tmp_path / "state").symlink_to(state_symlink_target, target_is_directory=True)
    env_path.chmod(env_mode)
    source = env_path.read_text(encoding="utf-8").replace(
        "DS_ENVIRONMENT_ID=dogfood",
        f"DS_ENVIRONMENT_ID={environment_id}",
    )
    if state_path is not None:
        source = source.replace(
            f"DOGFOOD_STATE_DIR={tmp_path / 'state'}",
            f"DOGFOOD_STATE_DIR={state_path}",
        )
    env_path.write_text(source, encoding="utf-8")
    initial_clone_assets: Path | None = None
    if clone_scenario == "existing":
        prepare_bootstrap_clone(tmp_path)
    else:
        initial_clone_assets = prepare_initial_bootstrap_clone_assets(tmp_path)
    fake_bin, call_log = install_bootstrap_command_fakes(tmp_path)
    environment = {
        **os.environ,
        "DOGFOOD_ENV_FILE": str(env_path),
        "WSL_DISTRO_NAME": wsl_distribution,
        "BOOTSTRAP_CALL_LOG": str(call_log),
        "BOOTSTRAP_GROUP_CREATED": str(tmp_path / "service-group.created"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    if failure is not None:
        environment["BOOTSTRAP_FAILURE"] = failure
    if docker_member:
        environment["BOOTSTRAP_DOCKER_MEMBER"] = "1"
    if initial_clone_assets is not None:
        environment["BOOTSTRAP_INITIAL_CLONE_ASSETS"] = str(initial_clone_assets)
    if service_group_missing:
        environment["BOOTSTRAP_SERVICE_GROUP_MISSING"] = "1"
    revision_path = tmp_path / "config" / "dogfood.revision"
    command = command_with_root_owned_revision(
        revision_path, [str(DOGFOOD_SCRIPTS_DIR / "bootstrap.sh")]
    )
    if non_root_parent is not None:
        command = [
            "fakeroot",
            "bash",
            "-c",
            'owner=$1; shift; /usr/bin/chown 1234 "$owner"; exec "$@"',
            "bash",
            str(non_root_parent),
            *command,
        ]
    result = subprocess.run(
        command,
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
            call.startswith(("chown\t", "chmod\t")) and not targets_temporary_path(call)
        )
        or call.startswith("systemctl\t")
        or (call.startswith("install\t") and not call.endswith("/backups"))
    )


def _git_fetch_calls(calls: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        call
        for call in calls
        if call.startswith("git\t") and " fetch --depth 1 origin " in call
    )


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


def test_should_place_assets_only_after_bootstrap_trust_checks_succeed(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        True,
        "existing",
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


def test_should_reject_a_symlinked_manifest_root_before_bootstrap_changes(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("unchanged", encoding="utf-8")

    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        state_symlink_target=outside,
    )

    assert result.returncode != 0
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert not any(
        call.startswith(("chown\t", "chmod\t", "install\t", "systemctl\t"))
        for call in calls
    )


@pytest.mark.parametrize("unsafe_parent", ("symlink", "non-root-owner"))
def test_should_reject_an_unsafe_manifest_parent_before_bootstrap_changes(
    tmp_path: Path,
    unsafe_parent: str,
) -> None:
    manifest_root = tmp_path / "manifest-root"
    manifest_root.mkdir()
    intermediate = manifest_root / "intermediate"
    non_root_parent = None
    if unsafe_parent == "symlink":
        intermediate.symlink_to(tmp_path, target_is_directory=True)
    else:
        intermediate.mkdir()
        non_root_parent = intermediate

    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        state_path=intermediate / "state",
        non_root_parent=non_root_parent,
    )

    assert result.returncode != 0
    assert not any(
        call.startswith(("chown\t", "chmod\t", "install\t", "systemctl\t"))
        for call in calls
    )


def test_should_stop_before_fetch_when_bootstrap_origin_does_not_match(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        "origin",
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode != 0
    assert len(_git_origin_lookup_calls(calls)) == 1
    assert _git_fetch_calls(calls) == ()


def test_secret_env_01_removes_temporary_environment_after_successful_bootstrap(
    tmp_path: Path,
) -> None:
    temporary_environment = tmp_path / "dogfood.env"

    result, _calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode == 0, result.stderr
    assert not temporary_environment.exists()


def test_should_place_assets_after_initial_clone_trust_checks_succeed(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "initial",
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


def test_should_create_initial_identity_before_reading_revision(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "initial",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        service_group_missing=True,
    )

    assert result.returncode == 0, result.stderr
    groupadd_index = calls.index(f"groupadd\t--system {TEST_SERVICE_GROUP}")
    revision_read_index = next(
        index
        for index, call in enumerate(calls)
        if call.startswith("python3\t- ") and "dogfood.revision" in call
    )
    assert groupadd_index < revision_read_index
    assert "renderer" in calls


def test_should_stop_before_asset_placement_when_initial_clone_fails(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        "clone",
        False,
        "initial",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode != 0
    assert any(call.startswith("git\tclone --no-checkout ") for call in calls)
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
        environment_id=environment_id,
        wsl_distribution=wsl_distribution,
    )

    assert result.returncode != 0
    assert len(calls) == 1
    assert calls[0].startswith("python3\t- ")
    assert _post_gate_side_effect_calls(calls, ()) == ()
    for path in ("clone", "state", "log"):
        assert not (tmp_path / path).exists()
    assert (tmp_path / "config" / "dogfood.revision").is_file()


def test_secret_env_01_stops_bootstrap_before_persistent_side_effects_for_exposed_env(
    tmp_path: Path,
) -> None:
    secret = "ab" * 32

    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "initial",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        env_mode=0o640,
    )

    assert result.returncode != 0
    assert len(calls) == 1
    assert calls[0].startswith("python3\t- ")
    assert _post_gate_side_effect_calls(calls, ()) == ()
    assert secret not in result.stdout
    assert secret not in result.stderr
    for path in ("clone", "state", "log", "backups"):
        assert not (tmp_path / path).exists()
    assert (tmp_path / "config" / "dogfood.revision").is_file()


def test_bootstrap_secret_keep_does_not_expose_authentication_key(
    tmp_path: Path,
) -> None:
    secret = "ab" * 32

    result, calls = _run_bootstrap(
        tmp_path,
        None,
        True,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert all(secret not in call for call in calls)
    assert (
        f"install\t-m 0640 -o root -g {TEST_SERVICE_GROUP} "
        f"{tmp_path / 'dogfood.env'} {tmp_path / 'config' / 'dogfood.env'}"
    ) in calls

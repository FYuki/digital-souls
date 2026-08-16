from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Literal

import pytest

from tests.dogfood_infrastructure_test_support import (
    TEST_REVISION,
    TEST_SECRET_SENTINEL,
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
        f"{action} digital-souls-dogfood.target\n"
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
    compose_available: bool = True,
    rerun: bool = False,
    initial_user_state: tuple[str, str, str] | None = None,
    data_root_residuals: dict[str, str] | None = None,
    node_version: str = "v22.0.0",
    node_version_exit_code: int = 0,
    missing_command: Literal["node", "npm"] | None = None,
    current_head: str | None = None,
    npm_dirty: bool = False,
    build_dirty: bool = False,
    persistent_sentinels: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], tuple[str, ...]]:
    env_path, data_dir = write_dogfood_env(tmp_path)
    if data_root_residuals is not None:
        for relative_path, content in data_root_residuals.items():
            residual = data_dir / relative_path
            residual.parent.mkdir(parents=True, exist_ok=True)
            residual.write_text(content, encoding="utf-8")
    if state_symlink_target is not None:
        (tmp_path / "state").symlink_to(state_symlink_target, target_is_directory=True)
    env_path.chmod(env_mode)
    source = env_path.read_text(encoding="utf-8").replace(
        "DS_ENVIRONMENT_ID=dogfood",
        f"DS_ENVIRONMENT_ID={environment_id}",
    )
    source += f"\nDOGFOOD_SERVICE_HOME_DIR={tmp_path / 'service-home'}\n"
    if persistent_sentinels is not None:
        source += f"DOGFOOD_OLLAMA_MODELS_DIR={tmp_path / 'ollama-models'}\n"
        sentinel_roots = {
            "data": data_dir,
            "backup": tmp_path / "backups",
            "model": tmp_path / "ollama-models",
        }
        for asset, content in persistent_sentinels.items():
            sentinel = sentinel_roots[asset] / f"{asset}.sentinel"
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(content, encoding="utf-8")
    if state_path is not None:
        source = source.replace(
            f"DOGFOOD_STATE_DIR={tmp_path / 'state'}",
            f"DOGFOOD_STATE_DIR={state_path}",
        )
    env_path.write_text(source, encoding="utf-8")
    environment_source = env_path.read_text(encoding="utf-8")
    initial_clone_assets: Path | None = None
    if clone_scenario == "existing":
        prepare_bootstrap_clone(tmp_path)
    else:
        initial_clone_assets = prepare_initial_bootstrap_clone_assets(tmp_path)
    fake_bin, call_log = install_bootstrap_command_fakes(tmp_path)
    if missing_command is not None:
        (fake_bin / missing_command).unlink()
    user_state_path = tmp_path / "service-user.state"
    user_state = initial_user_state or (
        str(tmp_path / "service-home"),
        TEST_SERVICE_GROUP,
        "/usr/sbin/nologin",
    )
    user_state_path.write_text(f"{'|'.join(user_state)}\n", encoding="utf-8")
    environment = {
        **os.environ,
        "DOGFOOD_ENV_FILE": str(env_path),
        "WSL_DISTRO_NAME": wsl_distribution,
        "BOOTSTRAP_CALL_LOG": str(call_log),
        "BOOTSTRAP_GROUP_CREATED": str(tmp_path / "service-group.created"),
        "BOOTSTRAP_USER_STATE": str(user_state_path),
        "BOOTSTRAP_NODE_VERSION": node_version,
        "BOOTSTRAP_NODE_VERSION_EXIT_CODE": str(node_version_exit_code),
        "BOOTSTRAP_CHECKED_OUT_MARKER": str(tmp_path / "checkout.complete"),
        "BOOTSTRAP_NPM_DIRTY_MARKER": str(tmp_path / "npm.dirty"),
        "BOOTSTRAP_BUILD_DIRTY_MARKER": str(tmp_path / "build.dirty"),
        "PATH": (
            f"{fake_bin}{os.pathsep}/usr/bin"
            if missing_command is not None
            else f"{fake_bin}{os.pathsep}{os.environ['PATH']}"
        ),
    }
    if current_head is not None:
        environment["BOOTSTRAP_CURRENT_HEAD"] = current_head
    if npm_dirty:
        environment["BOOTSTRAP_NPM_DIRTY"] = "1"
    if build_dirty:
        environment["BOOTSTRAP_BUILD_DIRTY"] = "1"
    if failure is not None:
        environment["BOOTSTRAP_FAILURE"] = failure
    if docker_member:
        environment["BOOTSTRAP_DOCKER_MEMBER"] = "1"
    if initial_clone_assets is not None:
        environment["BOOTSTRAP_INITIAL_CLONE_ASSETS"] = str(initial_clone_assets)
    if service_group_missing:
        environment["BOOTSTRAP_SERVICE_GROUP_MISSING"] = "1"
    if compose_available:
        environment["BOOTSTRAP_COMPOSE_AVAILABLE"] = "1"
    revision_path = tmp_path / "config" / "dogfood.revision"
    command = command_with_root_owned_revision(
        revision_path, [str(DOGFOOD_SCRIPTS_DIR / "bootstrap.sh")]
    )
    if non_root_parent is not None:
        command = [
            "fakeroot",
            "bash",
            "-c",
            '/usr/bin/chown "0:$1" "$2" 2>/dev/null '
            '|| [ "$(/usr/bin/stat -c %u:%g "$2")" = "0:$1" ]; '
            '/usr/bin/chown 1234 "$3"; shift 3; exec "$@"',
            "bash",
            str(os.getgid()),
            str(revision_path),
            str(non_root_parent),
            str(DOGFOOD_SCRIPTS_DIR / "bootstrap.sh"),
        ]
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
    )
    if rerun and result.returncode == 0:
        env_path.write_text(environment_source, encoding="utf-8")
        env_path.chmod(env_mode)
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
        if call.startswith("git\t") and " fetch origin " in call
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
    assert "docker\tcompose version" in calls
    assert "renderer" in calls
    assert _backup_calls(calls) == ()
    assert _target_cli_calls(calls) == ()
    assert "gpasswd\t--delete digital-souls docker" in calls
    assert any(call.startswith("install\t") for call in calls)
    assert "systemctl\tdaemon-reload" in calls
    assert "systemctl\tenable digital-souls-dogfood.target" in calls


def test_should_prepare_backend_and_frontend_without_starting_services(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode == 0, result.stderr
    assert calls.count("backend-setup") == 1
    assert any(call.startswith("npm\t") and " ci " in f" {call} " for call in calls)
    assert any(call.startswith("npm\t") and " run build" in call for call in calls)
    systemctl_actions = tuple(
        call.partition("\t")[2] for call in calls if call.startswith("systemctl\t")
    )
    assert not any(
        action.startswith(("start ", "restart ")) for action in systemctl_actions
    )


def test_should_converge_existing_user_and_directories_only_once_across_reruns(
    tmp_path: Path,
) -> None:
    residuals = {
        ".ollama/identity.sentinel": "ollama identity remains unchanged\n",
        ".cache/runtime.sentinel": "cache remains unchanged\n",
    }
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        rerun=True,
        initial_user_state=(str(tmp_path / "data"), "legacy-group", "/bin/bash"),
        data_root_residuals=residuals,
    )

    assert result.returncode == 0, result.stderr
    usermod_calls = tuple(call for call in calls if call.startswith("usermod\t"))
    assert usermod_calls == (
        "usermod\t"
        f"--home {tmp_path / 'service-home'} --gid {TEST_SERVICE_GROUP} "
        "--shell /usr/sbin/nologin digital-souls",
    )
    assert " -m " not in f" {usermod_calls[0]} "
    assert (tmp_path / "service-user.state").read_text(encoding="utf-8") == (
        f"{tmp_path / 'service-home'}|{TEST_SERVICE_GROUP}|/usr/sbin/nologin\n"
    )
    directory_installs = tuple(
        call for call in calls if call.startswith("install\t-d -m 0750")
    )
    assert sum(str(tmp_path / "service-home") in call for call in directory_installs) == 2
    assert (
        sum(
            "/var/lib/digital-souls/models/ollama" in call
            for call in directory_installs
        )
        == 2
    )
    for relative_path, content in residuals.items():
        residual = tmp_path / "data" / relative_path
        assert residual.is_file()
        assert residual.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("clone_scenario", ("existing", "initial"))
def test_should_converge_service_git_trust_after_clone_and_home_are_ready(
    tmp_path: Path,
    clone_scenario: Literal["existing", "initial"],
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        clone_scenario,
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        persistent_sentinels={},
    )

    assert result.returncode == 0, result.stderr
    service_home = tmp_path / "service-home"
    service_gitconfig = service_home / ".gitconfig"
    assert service_gitconfig.is_file()
    safe_directories = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(service_gitconfig),
            "--get-all",
            "safe.directory",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert safe_directories == [str((tmp_path / "clone").resolve())]
    clone_ready_index = max(
        index
        for index, call in enumerate(calls)
        if call.startswith("git\t") and "rev-parse HEAD" in call
    )
    home_ready_index = next(
        index
        for index, call in enumerate(calls)
        if call.startswith("install\t-d ") and str(service_home) in call
    )
    trust_index = next(
        index
        for index, call in enumerate(calls)
        if call.startswith("git\tconfig --file ")
    )
    assert clone_ready_index < home_ready_index < trust_index


def test_should_reject_bootstrap_before_changes_when_compose_plugin_is_missing(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        compose_available=False,
    )

    assert result.returncode == 2
    assert "docker\tcompose version" in calls
    assert _post_gate_side_effect_calls(calls, ()) == ()


def test_should_reject_node_major_other_than_22_before_changes(tmp_path: Path) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        node_version="v20.19.0",
    )

    assert result.returncode == 2
    assert "v20.19.0" in result.stderr
    assert "22" in result.stderr
    assert _post_gate_side_effect_calls(calls, ()) == ()


def test_should_reject_failed_node_version_detection_before_changes(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        node_version_exit_code=17,
    )

    assert result.returncode == 2
    assert "Node.js" in result.stderr
    assert "22" in result.stderr
    assert _post_gate_side_effect_calls(calls, ()) == ()


@pytest.mark.parametrize("missing_command", ("node", "npm"))
def test_should_reject_missing_node_or_npm_before_changes(
    tmp_path: Path,
    missing_command: Literal["node", "npm"],
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        missing_command=missing_command,
    )

    assert result.returncode == 2
    assert missing_command in result.stderr
    assert _post_gate_side_effect_calls(calls, ()) == ()


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


def test_should_converge_clean_detached_existing_clone_to_requested_revision(
    tmp_path: Path,
) -> None:
    old_revision = "1" * 40
    sentinels = {
        "data": "conversation data remains unchanged\n",
        "backup": "backup remains unchanged\n",
        "model": "ollama model remains unchanged\n",
    }
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        current_head=old_revision,
        persistent_sentinels=sentinels,
    )

    assert result.returncode == 0, result.stderr
    git_calls = tuple(call for call in calls if call.startswith("git\t"))
    fetch_calls = tuple(call for call in git_calls if " fetch origin " in call)
    checkout_calls = tuple(call for call in git_calls if "checkout --detach" in call)
    assert fetch_calls
    assert checkout_calls
    fetch_index = git_calls.index(fetch_calls[0])
    checkout_index = git_calls.index(checkout_calls[0])
    assert fetch_index < checkout_index
    assert any("rev-parse HEAD" in call for call in git_calls[:checkout_index])
    assert any("rev-parse HEAD" in call for call in git_calls[checkout_index + 1 :])
    sentinel_directories = {
        "data": "data",
        "backup": "backups",
        "model": "ollama-models",
    }
    for asset, content in sentinels.items():
        root = tmp_path / sentinel_directories[asset]
        assert (root / f"{asset}.sentinel").read_text(encoding="utf-8") == content
    assert TEST_SECRET_SENTINEL not in result.stdout
    assert TEST_SECRET_SENTINEL not in result.stderr
    assert all(TEST_SECRET_SENTINEL not in call for call in calls)


def test_should_report_dirty_existing_clone_without_checkout_or_destructive_git(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        "dirty",
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        current_head="1" * 40,
    )

    assert result.returncode != 0
    assert "M modified" in result.stderr or "M modified" in result.stdout
    git_arguments = tuple(call.partition("\t")[2] for call in calls if call.startswith("git\t"))
    assert not any("checkout" in arguments for arguments in git_arguments)
    assert not any(
        forbidden in arguments
        for arguments in git_arguments
        for forbidden in ("reset --hard", "clean -fdx")
    )


def test_should_report_branched_existing_clone_without_checkout_or_destructive_git(
    tmp_path: Path,
) -> None:
    current_head = "1" * 40
    result, calls = _run_bootstrap(
        tmp_path,
        "branch",
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        current_head=current_head,
    )

    assert result.returncode != 0
    diagnostics = f"{result.stdout}\n{result.stderr}"
    assert current_head in diagnostics
    assert TEST_REVISION in diagnostics
    git_arguments = tuple(
        call.partition("\t")[2] for call in calls if call.startswith("git\t")
    )
    assert not any("checkout" in arguments for arguments in git_arguments)
    assert not any(
        forbidden in arguments
        for arguments in git_arguments
        for forbidden in ("reset --hard", "clean -fdx")
    )


def test_should_stop_when_npm_ci_leaves_checkout_dirty(tmp_path: Path) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        npm_dirty=True,
    )

    assert result.returncode != 0
    assert any(call.startswith("npm\t") and " ci " in f" {call} " for call in calls)
    assert "frontend/package-lock.json" in result.stderr or "frontend/package-lock.json" in result.stdout
    assert not any(
        forbidden in call
        for call in calls
        for forbidden in ("reset --hard", "clean -fdx")
    )


def test_should_stop_before_read_only_permissions_when_frontend_build_is_dirty(
    tmp_path: Path,
) -> None:
    result, calls = _run_bootstrap(
        tmp_path,
        None,
        False,
        "existing",
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
        build_dirty=True,
    )

    build_index = next(
        index
        for index, call in enumerate(calls)
        if call.startswith("npm\t") and " run build" in call
    )
    later_calls = calls[build_index + 1 :]
    assert result.returncode != 0
    assert "frontend/build-runtime-artifact" in result.stderr
    assert not any(
        call.startswith(("chown\t", "chmod\t"))
        and str(tmp_path / "clone") in call
        for call in later_calls
    )


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
        "fetch origin",
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
    assert "systemctl\tenable digital-souls-dogfood.target" in calls


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

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal

import pytest

from tests.dogfood_infrastructure_test_support import (
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
BOOTSTRAP_FAILURES = ("fetch", "revision", "branch", "dirty", "origin")


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
    environment_id: str,
    wsl_distribution: str,
) -> tuple[subprocess.CompletedProcess[str], tuple[str, ...]]:
    env_path, _ = write_dogfood_env(tmp_path)
    source = env_path.read_text(encoding="utf-8").replace(
        "DS_ENVIRONMENT_ID=dogfood",
        f"DS_ENVIRONMENT_ID={environment_id}",
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
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    if failure is not None:
        environment["BOOTSTRAP_FAILURE"] = failure
    if docker_member:
        environment["BOOTSTRAP_DOCKER_MEMBER"] = "1"
    if initial_clone_assets is not None:
        environment["BOOTSTRAP_INITIAL_CLONE_ASSETS"] = str(initial_clone_assets)
    result = subprocess.run(
        [str(DOGFOOD_SCRIPTS_DIR / "bootstrap.sh")],
        env=environment,
        capture_output=True,
        text=True,
    )
    calls = tuple(call_log.read_text(encoding="utf-8").splitlines())
    return result, calls


def _side_effect_calls(calls: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        call
        for call in calls
        if call == "renderer"
        or call.startswith(("chown\t", "chmod\t", "install\t", "systemctl\t"))
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
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode != 0
    assert _side_effect_calls(calls) == ()


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
        environment_id="dogfood",
        wsl_distribution="Ubuntu-dogfood",
    )

    assert result.returncode != 0
    assert any(
        call.startswith("git\tclone --no-checkout ")
        for call in calls
    )
    assert _side_effect_calls(calls) == ()


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
    assert calls == ()
    for path in ("clone", "config", "state", "log"):
        assert not (tmp_path / path).exists()

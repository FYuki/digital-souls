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


from tests.dogfood_deploy_test_support import (
    NEXT_REVISION,
    THIRD_REVISION,
    CONVERSATION_SENTINEL,
    PROMPT_SENTINEL,
    ROOT_OPERATION_CASES,
    ROOT_GUARD_POST_COMMANDS,
    _manifest_payload,
    _legacy_manifest_payload,
    _install_rejection_fakes,
    _prepare_deploy_scenario,
    _invoke_deployment_contract_migration,
    _run_deploy,
    _invoke_deploy,
    _read_log_records,
    _run_service_git_trust_convergence,
    _read_effective_global_safe_directories,
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
        {"url": "http://localhost:18000/health/ready", "timeoutSeconds": 2.0},
    ]


def test_should_select_service_git_config_for_backup_and_backup_verify(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(tmp_path)

    assert result.returncode == 0, (result.stdout, result.stderr)
    backup_home_observations = tuple(
        call
        for call in calls
        if call.startswith(("cli-home\tbackup\t", "cli-home\tbackup-verify\t"))
    )
    service_home = tmp_path / "service-home"
    assert backup_home_observations == (
        f"cli-home\tbackup\t{service_home}",
        f"cli-home\tbackup-verify\t{service_home}",
    )
    backup_gitconfig_observations = tuple(
        call
        for call in calls
        if call.startswith(("cli-gitconfig\tbackup\t", "cli-gitconfig\tbackup-verify\t"))
    )
    service_gitconfig = service_home / ".gitconfig"
    assert backup_gitconfig_observations == (
        f"cli-gitconfig\tbackup\t{service_gitconfig}",
        f"cli-gitconfig\tbackup-verify\t{service_gitconfig}",
    )


def test_should_exclude_xdg_safe_directory_from_selected_service_git_config(
    tmp_path: Path,
) -> None:
    service_home = tmp_path / "service-home"
    service_home.mkdir()
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    service_gitconfig = service_home / ".gitconfig"
    service_gitconfig.write_text(
        f"[safe]\n\tdirectory = {clone_dir}\n",
        encoding="utf-8",
    )
    xdg_gitconfig = service_home / ".config" / "git" / "config"
    xdg_gitconfig.parent.mkdir(parents=True)
    xdg_gitconfig.write_text("[safe]\n\tdirectory = *\n", encoding="utf-8")

    safe_directories = _read_effective_global_safe_directories(
        service_home,
        clone_dir,
    )

    assert safe_directories == [str(clone_dir)]


def test_should_converge_only_the_normalized_clone_in_service_git_config(
    tmp_path: Path,
) -> None:
    service_home = tmp_path / "service-home"
    service_home.mkdir()
    actual_clone = tmp_path / "actual-clone"
    actual_clone.mkdir()
    configured_clone = tmp_path / "configured-clone"
    configured_clone.symlink_to(actual_clone, target_is_directory=True)
    service_gitconfig = service_home / ".gitconfig"
    subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(service_gitconfig),
            "user.name",
            "preserved-user",
        ],
        check=True,
    )
    for unsafe_value in ("*", str(tmp_path / "other-clone")):
        subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(service_gitconfig),
                "--add",
                "safe.directory",
                unsafe_value,
            ],
            check=True,
        )
    service_gitconfig.chmod(0o666)
    ambient_home = tmp_path / "ambient-home"
    ambient_home.mkdir()
    ambient_gitconfig = ambient_home / ".gitconfig"
    ambient_gitconfig.write_text("[user]\n\tname = ambient-user\n", encoding="utf-8")
    ambient_before = ambient_gitconfig.read_bytes()
    result = _run_service_git_trust_convergence(
        service_home,
        configured_clone,
        {**os.environ, "HOME": str(ambient_home)},
        repeat=True,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    safe_directories = _read_effective_global_safe_directories(
        service_home,
        actual_clone,
    )
    assert safe_directories == [str(actual_clone.resolve())]
    preserved_name = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(service_gitconfig),
            "--get",
            "user.name",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert preserved_name == "preserved-user"
    assert ambient_gitconfig.read_bytes() == ambient_before
    metadata = service_gitconfig.stat()
    assert metadata.st_uid == os.getuid()
    assert metadata.st_gid == os.getgid()
    assert metadata.st_mode & 0o777 == 0o640


def test_should_preserve_service_git_config_when_clone_cannot_be_resolved(
    tmp_path: Path,
) -> None:
    service_home = tmp_path / "service-home"
    service_home.mkdir()
    service_gitconfig = service_home / ".gitconfig"
    service_gitconfig.write_text(
        "[safe]\n\tdirectory = *\n[user]\n\tname = preserved-user\n",
        encoding="utf-8",
    )
    config_before = service_gitconfig.read_bytes()
    result = _run_service_git_trust_convergence(
        service_home,
        tmp_path / "missing-parent" / "missing-clone",
        os.environ.copy(),
        repeat=False,
    )

    assert result.returncode != 0
    assert service_gitconfig.read_bytes() == config_before


def test_should_reject_service_git_config_symlink_without_changing_its_target(
    tmp_path: Path,
) -> None:
    service_home = tmp_path / "service-home"
    service_home.mkdir()
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    symlink_target = tmp_path / "protected-config"
    symlink_target.write_text("protected\n", encoding="utf-8")
    symlink_target.chmod(0o600)
    target_before = (symlink_target.read_bytes(), symlink_target.stat())
    (service_home / ".gitconfig").symlink_to(symlink_target)

    result = _run_service_git_trust_convergence(
        service_home,
        clone_dir,
        os.environ.copy(),
        repeat=False,
    )

    assert result.returncode != 0
    assert (service_home / ".gitconfig").is_symlink()
    target_after = symlink_target.stat()
    assert symlink_target.read_bytes() == target_before[0]
    assert (target_after.st_uid, target_after.st_gid, target_after.st_mode) == (
        target_before[1].st_uid,
        target_before[1].st_gid,
        target_before[1].st_mode,
    )


def test_should_reject_service_git_config_replaced_by_symlink_during_convergence(
    tmp_path: Path,
) -> None:
    service_home = tmp_path / "service-home"
    service_home.mkdir()
    service_gitconfig = service_home / ".gitconfig"
    service_gitconfig.write_text("[user]\n\tname = preserved-user\n", encoding="utf-8")
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    symlink_target = tmp_path / "protected-config"
    symlink_target.write_text("protected\n", encoding="utf-8")
    symlink_target.chmod(0o600)
    target_before = (symlink_target.read_bytes(), symlink_target.stat())
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_executable(
        fake_bin / "git",
        '/usr/bin/git "$@"\nln -sfn "$GITCONFIG_SWAP_TARGET" "$SERVICE_GITCONFIG"\n',
    )

    result = _run_service_git_trust_convergence(
        service_home,
        clone_dir,
        {
            **os.environ,
            "GITCONFIG_SWAP_TARGET": str(symlink_target),
            "SERVICE_GITCONFIG": str(service_gitconfig),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
        repeat=False,
    )

    assert result.returncode != 0
    assert service_gitconfig.is_symlink()
    target_after = symlink_target.stat()
    assert symlink_target.read_bytes() == target_before[0]
    assert (target_after.st_uid, target_after.st_gid, target_after.st_mode) == (
        target_before[1].st_uid,
        target_before[1].st_gid,
        target_before[1].st_mode,
    )


def test_should_reject_service_home_symlink_without_changing_its_target(
    tmp_path: Path,
) -> None:
    protected_home = tmp_path / "protected-home"
    protected_home.mkdir()
    protected_gitconfig = protected_home / ".gitconfig"
    protected_gitconfig.write_text("protected\n", encoding="utf-8")
    protected_gitconfig.chmod(0o600)
    target_before = (protected_gitconfig.read_bytes(), protected_gitconfig.stat())
    service_home = tmp_path / "service-home"
    service_home.symlink_to(protected_home, target_is_directory=True)
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    result = _run_service_git_trust_convergence(
        service_home,
        clone_dir,
        os.environ.copy(),
        repeat=False,
    )

    assert result.returncode != 0
    assert service_home.is_symlink()
    target_after = protected_gitconfig.stat()
    assert protected_gitconfig.read_bytes() == target_before[0]
    assert (target_after.st_uid, target_after.st_gid, target_after.st_mode) == (
        target_before[1].st_uid,
        target_before[1].st_gid,
        target_before[1].st_mode,
    )


def test_should_reject_service_home_replaced_by_symlink_during_convergence(
    tmp_path: Path,
) -> None:
    service_home = tmp_path / "service-home"
    service_home.mkdir()
    original_home = tmp_path / "original-home"
    protected_home = tmp_path / "protected-home"
    protected_home.mkdir()
    protected_gitconfig = protected_home / ".gitconfig"
    protected_gitconfig.write_text("protected\n", encoding="utf-8")
    protected_gitconfig.chmod(0o600)
    target_before = (protected_gitconfig.read_bytes(), protected_gitconfig.stat())
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_executable(
        fake_bin / "mv",
        '/usr/bin/mv -- "$SERVICE_HOME" "$ORIGINAL_HOME"\n'
        'ln -s -- "$PROTECTED_HOME" "$SERVICE_HOME"\n'
        'exec /usr/bin/mv "$@"\n',
    )

    result = _run_service_git_trust_convergence(
        service_home,
        clone_dir,
        {
            **os.environ,
            "ORIGINAL_HOME": str(original_home),
            "PROTECTED_HOME": str(protected_home),
            "SERVICE_HOME": str(service_home),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
        repeat=False,
    )

    assert result.returncode != 0
    assert service_home.is_symlink()
    assert original_home.is_dir()
    target_after = protected_gitconfig.stat()
    assert protected_gitconfig.read_bytes() == target_before[0]
    assert (target_after.st_uid, target_after.st_gid, target_after.st_mode) == (
        target_before[1].st_uid,
        target_before[1].st_gid,
        target_before[1].st_mode,
    )


@pytest.mark.parametrize("unsafe_value", ("*", "other-clone"))
def test_should_reject_included_safe_directory_without_changing_primary_config(
    tmp_path: Path,
    unsafe_value: str,
) -> None:
    service_home = tmp_path / "service-home"
    service_home.mkdir()
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    included_gitconfig = service_home / "included.gitconfig"
    included_value = (
        unsafe_value if unsafe_value == "*" else str(tmp_path / unsafe_value)
    )
    included_gitconfig.write_text(
        f"[safe]\n\tdirectory = {included_value}\n",
        encoding="utf-8",
    )
    service_gitconfig = service_home / ".gitconfig"
    service_gitconfig.write_text(
        "[include]\n\tpath = included.gitconfig\n[user]\n\tname = preserved-user\n",
        encoding="utf-8",
    )
    service_gitconfig.chmod(0o600)
    config_before = (service_gitconfig.read_bytes(), service_gitconfig.stat())
    assert _read_effective_global_safe_directories(service_home, clone_dir) == [
        included_value
    ]

    result = _run_service_git_trust_convergence(
        service_home,
        clone_dir,
        os.environ.copy(),
        repeat=False,
    )

    assert result.returncode != 0
    config_after = service_gitconfig.stat()
    assert service_gitconfig.read_bytes() == config_before[0]
    assert (config_after.st_uid, config_after.st_gid, config_after.st_mode) == (
        config_before[1].st_uid,
        config_before[1].st_gid,
        config_before[1].st_mode,
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
                "restart",
                "cli\twait-readiness ",
            )
        ),
    )
    assert operations == tuple(sorted(operations))
    readiness_calls = tuple(
        call for call in calls if call.startswith("cli\twait-readiness ")
    )
    assert readiness_calls == (
        "cli\twait-readiness --profile dogfood --service frontend "
        "--service backend --max-attempts 180 --interval-seconds 1 "
        "--request-timeout-seconds 2",
    )
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{NEXT_REVISION}\n"
    assert (tmp_path / "config" / "dogfood-images.env").read_text(
        encoding="utf-8"
    ) == (
        f"DOGFOOD_BACKEND_IMAGE={TEST_DEPLOYMENT_IMAGES['backend']}\n"
        f"DOGFOOD_FRONTEND_IMAGE={TEST_DEPLOYMENT_IMAGES['frontend']}\n"
        f"DOGFOOD_WHISPER_IMAGE={TEST_DEPLOYMENT_IMAGES['whisper']}\n"
    )
    generations = tuple((tmp_path / "state" / "deployments").glob("*.json"))
    generation = next(path for path in generations if path.name != "current.json")
    read_valid_deployment_manifest(
        generation,
        {
            "previousCommit": TEST_REVISION,
            "targetCommit": NEXT_REVISION,
            "profileSchemaVersion": 1,
            "dataSchemaVersion": 3,
            "backupId": str(
                tmp_path / "backups" / "backup-20260906T000000Z-0123456789ab-abcdef012345"
            ),
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


def test_should_record_null_previous_commit_on_the_true_initial_deploy(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(
        tmp_path,
        target_revision=TEST_REVISION,
        deployment_revision=None,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    required_operations = (
        "cli\tbackup ",
        "cli\tbackup-verify ",
        "manifest-write",
        "restart",
        "cli\twait-readiness ",
    )
    assert all(any(marker in call for call in calls) for marker in required_operations)
    generations = tuple((tmp_path / "state" / "deployments").glob("*.json"))
    generation = next(path for path in generations if path.name != "current.json")
    expected_manifest = {
        "previousCommit": None,
        "targetCommit": TEST_REVISION,
        "profileSchemaVersion": 1,
        "dataSchemaVersion": 3,
        "backupId": str(
            tmp_path / "backups" / "backup-20260906T000000Z-0123456789ab-abcdef012345"
        ),
    }
    read_valid_deployment_manifest(generation, expected_manifest)
    read_valid_deployment_manifest(
        tmp_path / "state" / "deployments" / "current.json",
        expected_manifest,
    )


def test_should_restore_previous_commit_from_revision_after_bootstrap_checkout(
    tmp_path: Path,
) -> None:
    result, _ = _run_deploy(
        tmp_path,
        head_revision=NEXT_REVISION,
        deployment_revision=TEST_REVISION,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    manifest = json.loads(
        (tmp_path / "state" / "deployments" / "current.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["previousCommit"] == TEST_REVISION
    assert manifest["targetCommit"] == NEXT_REVISION


def test_should_keep_null_previous_commit_when_redeploying_the_initial_sha(
    tmp_path: Path,
) -> None:
    environment, _ = _prepare_deploy_scenario(
        tmp_path,
        target_revision=TEST_REVISION,
        deployment_revision=None,
    )

    first_result = _invoke_deploy(
        tmp_path,
        environment,
        ["--commit", TEST_REVISION],
        revision_exists=False,
    )
    second_result = _invoke_deploy(
        tmp_path,
        environment,
        ["--commit", TEST_REVISION],
        revision_exists=True,
    )

    assert first_result.returncode == 0, (first_result.stdout, first_result.stderr)
    assert second_result.returncode == 0, (
        second_result.stdout,
        second_result.stderr,
    )
    manifest = json.loads(
        (tmp_path / "state" / "deployments" / "current.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["previousCommit"] is None
    assert manifest["targetCommit"] == TEST_REVISION


def test_should_keep_the_true_previous_commit_across_repeated_same_sha_deploys(
    tmp_path: Path,
) -> None:
    current_manifest = _manifest_payload(TEST_REVISION, NEXT_REVISION)
    environment, _ = _prepare_deploy_scenario(
        tmp_path,
        head_revision=NEXT_REVISION,
        deployment_revision=NEXT_REVISION,
        current_manifest_payload=current_manifest,
    )

    results = tuple(
        _invoke_deploy(
            tmp_path,
            environment,
            ["--commit", NEXT_REVISION],
            revision_exists=True,
        )
        for _ in range(2)
    )

    assert all(result.returncode == 0 for result in results), tuple(
        (result.stdout, result.stderr) for result in results
    )
    manifest = json.loads(
        (tmp_path / "state" / "deployments" / "current.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["previousCommit"] == TEST_REVISION
    assert manifest["targetCommit"] == NEXT_REVISION


def test_should_use_manifest_target_when_it_differs_from_the_requested_target(
    tmp_path: Path,
) -> None:
    result, _ = _run_deploy(
        tmp_path,
        head_revision=NEXT_REVISION,
        deployment_revision=NEXT_REVISION,
        current_manifest_payload=_manifest_payload(THIRD_REVISION, TEST_REVISION),
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    manifest = json.loads(
        (tmp_path / "state" / "deployments" / "current.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["previousCommit"] == TEST_REVISION
    assert manifest["targetCommit"] == NEXT_REVISION


def test_should_reject_a_self_referencing_manifest_before_deploy_side_effects(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(
        tmp_path,
        head_revision=NEXT_REVISION,
        deployment_revision=NEXT_REVISION,
        current_manifest_payload=_manifest_payload(NEXT_REVISION, NEXT_REVISION),
    )

    diagnostic = result.stdout + result.stderr
    assert result.returncode != 0
    assert "引数なし" in diagnostic
    assert "手編集" in diagnostic
    assert "--to <SHA>" in diagnostic
    assert "infra/dogfood/README.md" in diagnostic
    assert not any(call.startswith("cli\tbackup ") for call in calls)
    assert not any("checkout --detach" in call for call in calls)
    assert {
        path.name for path in (tmp_path / "state" / "deployments").glob("*.json")
    } == {"current.json"}


def test_should_reject_a_self_referencing_revision_before_deploy_side_effects(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(
        tmp_path,
        head_revision=NEXT_REVISION,
        deployment_revision=NEXT_REVISION,
    )

    diagnostic = result.stdout + result.stderr
    assert result.returncode != 0
    assert "--to <SHA>" in diagnostic
    assert "infra/dogfood/README.md" in diagnostic
    assert not any(call.startswith("cli\tbackup ") for call in calls)
    assert not any("checkout --detach" in call for call in calls)
    assert not tuple((tmp_path / "state" / "deployments").glob("*.json"))


def test_should_migrate_legacy_manifests_and_establish_a_new_docker_baseline(
    tmp_path: Path,
) -> None:
    environment, _ = _prepare_deploy_scenario(
        tmp_path,
        current_manifest_payload=_legacy_manifest_payload(),
    )

    migration = _invoke_deployment_contract_migration(tmp_path, environment)

    assert migration.returncode == 0, (migration.stdout, migration.stderr)
    deployments = tmp_path / "state" / "deployments"
    assert not tuple(deployments.glob("*.json"))
    archives = tuple(deployments.glob("pre-migration-*-to-*"))
    assert len(archives) == 1
    legacy_current = json.loads(
        (archives[0] / "current.json").read_text(encoding="utf-8")
    )
    assert legacy_current == _legacy_manifest_payload()
    marker = tmp_path / "state" / "deployment-contract-migration.json"
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_payload["fromCommit"] == TEST_REVISION
    assert marker_payload["targetCommit"] == NEXT_REVISION
    assert marker_payload["archiveDirectory"] == archives[0].name
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{NEXT_REVISION}\n"

    # bootstrapが同じtargetへcheckoutした状態を再現する。
    (tmp_path / "head").write_text(NEXT_REVISION, encoding="utf-8")
    deploy = _invoke_deploy(
        tmp_path,
        environment,
        ["--commit", NEXT_REVISION],
        revision_exists=True,
    )

    assert deploy.returncode == 0, (deploy.stdout, deploy.stderr)
    current = json.loads((deployments / "current.json").read_text(encoding="utf-8"))
    assert current["previousCommit"] is None
    assert current["targetCommit"] == NEXT_REVISION
    assert not marker.exists()
    completed_marker = json.loads(
        (archives[0] / "migration.json").read_text(encoding="utf-8")
    )
    assert completed_marker["targetCommit"] == NEXT_REVISION


def test_should_not_rollback_across_the_legacy_contract_boundary(
    tmp_path: Path,
) -> None:
    environment, call_log = _prepare_deploy_scenario(
        tmp_path,
        failure="readiness",
        current_manifest_payload=_legacy_manifest_payload(),
    )
    migration = _invoke_deployment_contract_migration(tmp_path, environment)
    assert migration.returncode == 0, (migration.stdout, migration.stderr)
    (tmp_path / "head").write_text(NEXT_REVISION, encoding="utf-8")

    deploy = _invoke_deploy(
        tmp_path,
        environment,
        ["--commit", NEXT_REVISION],
        revision_exists=True,
    )

    calls = tuple(call_log.read_text(encoding="utf-8").splitlines())
    diagnostic = deploy.stdout + deploy.stderr
    assert deploy.returncode == 1
    assert "初回 deploy" in diagnostic
    assert "自動 rollback できない" in diagnostic
    assert sum("checkout --detach" in call for call in calls) == 1
    assert (tmp_path / "state" / "deployment-contract-migration.json").is_file()


def test_should_reject_a_tampered_migration_marker_before_backup(
    tmp_path: Path,
) -> None:
    environment, call_log = _prepare_deploy_scenario(
        tmp_path,
        current_manifest_payload=_legacy_manifest_payload(),
    )
    migration = _invoke_deployment_contract_migration(tmp_path, environment)
    assert migration.returncode == 0, (migration.stdout, migration.stderr)
    marker = tmp_path / "state" / "deployment-contract-migration.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["targetCommit"] = THIRD_REVISION
    marker.write_text(json.dumps(payload), encoding="utf-8")
    marker.chmod(0o640)
    (tmp_path / "head").write_text(NEXT_REVISION, encoding="utf-8")

    deploy = _invoke_deploy(
        tmp_path,
        environment,
        ["--commit", NEXT_REVISION],
        revision_exists=True,
    )

    calls = tuple(call_log.read_text(encoding="utf-8").splitlines())
    assert deploy.returncode == 2
    assert "migration markerを検証できません" in deploy.stderr
    assert not any(call.startswith("cli\tbackup ") for call in calls)
    assert not tuple((tmp_path / "state" / "deployments").glob("*.json"))


def test_should_reject_corrupt_legacy_manifest_without_changing_revision(
    tmp_path: Path,
) -> None:
    environment, _ = _prepare_deploy_scenario(
        tmp_path,
        current_manifest_payload="{not-json",
    )

    migration = _invoke_deployment_contract_migration(tmp_path, environment)

    assert migration.returncode == 2
    assert "deployment manifestを検証できません" in migration.stderr
    assert (tmp_path / "state" / "deployments" / "current.json").is_file()
    assert not (tmp_path / "state" / "deployment-contract-migration.json").exists()
    assert not tuple(
        (tmp_path / "state" / "deployments").glob("pre-migration-*-to-*")
    )
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{TEST_REVISION}\n"


def test_should_idempotently_prepare_migration_without_a_legacy_manifest(
    tmp_path: Path,
) -> None:
    environment, _ = _prepare_deploy_scenario(tmp_path)

    first = _invoke_deployment_contract_migration(tmp_path, environment)
    second = _invoke_deployment_contract_migration(tmp_path, environment)

    assert first.returncode == 0, (first.stdout, first.stderr)
    assert second.returncode == 0, (second.stdout, second.stderr)
    deployments = tmp_path / "state" / "deployments"
    archives = tuple(deployments.glob("pre-migration-*-to-*"))
    assert len(archives) == 1
    assert not tuple(archives[0].glob("*.json"))
    marker = tmp_path / "state" / "deployment-contract-migration.json"
    assert marker.is_file()
    assert json.loads(marker.read_text(encoding="utf-8"))["targetCommit"] == (
        NEXT_REVISION
    )
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{NEXT_REVISION}\n"


def test_should_migrate_a_current_image_aware_manifest(tmp_path: Path) -> None:
    environment, _ = _prepare_deploy_scenario(
        tmp_path,
        current_manifest_payload=_manifest_payload(TEST_REVISION, NEXT_REVISION),
    )

    migration = _invoke_deployment_contract_migration(tmp_path, environment)

    assert migration.returncode == 0, (migration.stdout, migration.stderr)
    deployments = tmp_path / "state" / "deployments"
    archives = tuple(deployments.glob("pre-migration-*-to-*"))
    assert len(archives) == 1
    archived = json.loads(
        (archives[0] / "current.json").read_text(encoding="utf-8")
    )
    assert archived == _manifest_payload(TEST_REVISION, NEXT_REVISION)
    assert not (deployments / "current.json").exists()
    marker = tmp_path / "state" / "deployment-contract-migration.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["schemaVersion"] == 2
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{NEXT_REVISION}\n"


def test_should_repair_a_self_referencing_manifest_during_a_normal_deploy(
    tmp_path: Path,
) -> None:
    result, _ = _run_deploy(
        tmp_path,
        current_manifest_payload=_manifest_payload(NEXT_REVISION, NEXT_REVISION),
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    manifest = json.loads(
        (tmp_path / "state" / "deployments" / "current.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["previousCommit"] == TEST_REVISION
    assert manifest["targetCommit"] == NEXT_REVISION


@pytest.mark.parametrize(
    "manifest_payload",
    (
        {
            "previousCommit": TEST_REVISION,
            "profileSchemaVersion": 1,
            "dataSchemaVersion": 3,
            "backupId": "backup-current",
            "deployedAt": "2026-07-31T00:00:00Z",
        },
        {
            "targetCommit": NEXT_REVISION,
            "profileSchemaVersion": 1,
            "dataSchemaVersion": 3,
            "backupId": "backup-current",
            "deployedAt": "2026-07-31T00:00:00Z",
        },
        "{not-json",
        _manifest_payload(
            TEST_REVISION,
            NEXT_REVISION,
            data_schema_version=4,
        ),
    ),
    ids=(
        "missing-target-commit",
        "missing-previous-commit",
        "invalid-json",
        "schema-mismatch",
    ),
)
def test_should_reject_an_invalid_manifest_before_deploy_side_effects(
    tmp_path: Path,
    manifest_payload: dict[str, object] | str,
) -> None:
    result, calls = _run_deploy(
        tmp_path,
        head_revision=NEXT_REVISION,
        deployment_revision=NEXT_REVISION,
        current_manifest_payload=manifest_payload,
    )

    diagnostic = result.stdout + result.stderr
    assert result.returncode != 0
    assert "--to <SHA>" in diagnostic
    assert "infra/dogfood/README.md" in diagnostic
    assert not any(call.startswith("cli\tbackup ") for call in calls)
    assert not any("checkout --detach" in call for call in calls)


def test_should_reject_an_unreadable_manifest_before_deploy_side_effects(
    tmp_path: Path,
) -> None:
    environment, call_log = _prepare_deploy_scenario(
        tmp_path,
        head_revision=NEXT_REVISION,
        deployment_revision=NEXT_REVISION,
        current_manifest_payload=_manifest_payload(TEST_REVISION, NEXT_REVISION),
    )
    (tmp_path / "state" / "deployments" / "current.json").chmod(0o000)

    result = _invoke_deploy(
        tmp_path,
        environment,
        ["--commit", NEXT_REVISION],
        revision_exists=True,
    )

    calls = (
        tuple(call_log.read_text(encoding="utf-8").splitlines())
        if call_log.exists()
        else ()
    )
    diagnostic = result.stdout + result.stderr
    assert result.returncode != 0
    assert "--to <SHA>" in diagnostic
    assert "infra/dogfood/README.md" in diagnostic
    assert not any(call.startswith("cli\tbackup ") for call in calls)
    assert not any("checkout --detach" in call for call in calls)

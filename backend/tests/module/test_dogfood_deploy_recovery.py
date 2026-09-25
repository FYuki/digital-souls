from __future__ import annotations

import json
import os
import pwd
import signal
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

@pytest.mark.parametrize(
    "manifest_payload",
    (
        _manifest_payload(None, NEXT_REVISION),
        {
            "targetCommit": NEXT_REVISION,
            "profileSchemaVersion": 1,
            "dataSchemaVersion": 3,
            "backupId": "backup-current",
            "deployedAt": "2026-07-31T00:00:00Z",
        },
    ),
    ids=("null", "missing"),
)
def test_should_report_an_unset_previous_commit_for_implicit_rollback(
    tmp_path: Path,
    manifest_payload: dict[str, object],
) -> None:
    environment, _ = _prepare_deploy_scenario(
        tmp_path,
        head_revision=NEXT_REVISION,
        deployment_revision=NEXT_REVISION,
        current_manifest_payload=manifest_payload,
    )

    result = subprocess.run(
        command_with_root_owned_revision(
            tmp_path / "config" / "dogfood.revision",
            [str(DOGFOOD_SCRIPTS_DIR / "rollback.sh")],
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert (
        "rollback 元が未設定です。`--to <SHA>` で保存済み世代を明示指定してください"
        in result.stdout + result.stderr
    )
    assert not (tmp_path / "checkout-count").exists()


def test_should_restore_deployment_state_revision_when_bootstrap_target_matches_head(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(
        tmp_path,
        failure="readiness",
        target_revision=TEST_REVISION,
        current_deployment_revision=NEXT_REVISION,
    )

    assert result.returncode != 0
    deployments = tmp_path / "state" / "deployments"
    generation_payloads = tuple(
        json.loads(path.read_text(encoding="utf-8"))
        for path in deployments.glob("*.json")
        if path.name != "current.json"
    )
    deploy_manifest = next(
        payload
        for payload in generation_payloads
        if payload["targetCommit"] == TEST_REVISION
    )
    assert deploy_manifest["previousCommit"] == NEXT_REVISION
    checkout_calls = tuple(call for call in calls if "checkout --detach" in call)
    assert checkout_calls[-1].endswith(NEXT_REVISION)
    assert (tmp_path / "head").read_text(encoding="utf-8") == NEXT_REVISION
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{NEXT_REVISION}\n"


@pytest.mark.parametrize("failure", (None, "readiness"), ids=("deploy", "rollback"))
def test_should_check_checkout_before_applying_read_only_clone_permissions(
    tmp_path: Path, failure: str | None
) -> None:
    result, calls = _run_deploy(tmp_path, failure=failure)

    checkout_indexes = tuple(
        index for index, call in enumerate(calls) if "checkout --detach" in call
    )
    assert checkout_indexes
    for checkout_index in checkout_indexes:
        next_checkout = next(
            (
                index
                for index in range(checkout_index + 1, len(calls))
                if "checkout --detach" in calls[index]
            ),
            len(calls),
        )
        following_activation_calls = calls[checkout_index + 1 : next_checkout]
        clean_indexes = tuple(
            index
            for index, call in enumerate(following_activation_calls)
            if call.startswith("git\t") and " status --porcelain" in call
        )
        permission_index = next(
            index
            for index, call in enumerate(following_activation_calls)
            if call.startswith(("chown\t", "chmod\t"))
        )
        assert clean_indexes, "checkout後のclean checkout確認が必要です"
        assert clean_indexes[0] < permission_index

    assert result.returncode == (0 if failure is None else 1)


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
        f"ExecStart={tmp_path / 'clone' / 'scripts' / 'start-dogfood.sh'}"
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
    assert "manifest-write" not in calls
    assert "revision-update" not in calls
    assert not tuple((tmp_path / "state" / "deployments").glob("*.json"))
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{TEST_REVISION}\n"


@pytest.mark.parametrize(
    ("failure", "last_operation"),
    (
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
    assert not any(call.startswith("cli\twait-readiness ") for call in calls)
    diagnostic = result.stdout + result.stderr
    assert f"現在のrevision: {TEST_REVISION}" in diagnostic
    assert f"現在のHEAD: {TEST_REVISION}" in diagnostic


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
        ("chown\t", "chown"),
        ("chmod\t", "chmod"),
        ("restart", "restart"),
        ("cli\twait-readiness ", "readiness"),
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
        "chown",
        "chmod",
        "restart",
        "readiness",
        "readiness-failure",
        "checkout",
        "backend-setup",
        "chown",
        "chmod",
        "restart",
        "readiness",
        "readiness-success",
    )
    assert (tmp_path / "config" / "dogfood.revision").read_text(
        encoding="utf-8"
    ) == f"{TEST_REVISION}\n"


def test_should_keep_the_initial_target_when_readiness_fails_without_a_previous_commit(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(
        tmp_path,
        failure="readiness",
        target_revision=TEST_REVISION,
        deployment_revision=None,
    )

    diagnostic = result.stdout + result.stderr
    assert result.returncode == 1
    assert "初回 deploy" in diagnostic
    assert "自動 rollback できない" in diagnostic
    assert "原因調査" in diagnostic
    assert "--to <SHA>" in diagnostic
    assert "再 deploy" in diagnostic
    assert sum("checkout --detach" in call for call in calls) == 1
    assert (tmp_path / "head").read_text(encoding="utf-8") == TEST_REVISION
    manifest = json.loads(
        (tmp_path / "state" / "deployments" / "current.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["previousCommit"] is None
    assert manifest["targetCommit"] == TEST_REVISION


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


def test_should_prioritize_explicit_rollback_suppression_on_initial_deploy_failure(
    tmp_path: Path,
) -> None:
    result, calls = _run_deploy(
        tmp_path,
        failure="readiness",
        no_auto_rollback=True,
        target_revision=TEST_REVISION,
        deployment_revision=None,
    )

    diagnostic = result.stdout + result.stderr
    assert result.returncode == 1
    assert "自動rollbackは抑止されています" in diagnostic
    assert "初回 deploy" not in diagnostic
    assert sum("checkout --detach" in call for call in calls) == 1


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
    ids=("bootstrap", "migration", "deploy", "rollback"),
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
    ids=("bootstrap", "migration", "deploy", "rollback"),
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
    ids=("bootstrap", "migration", "deploy", "rollback"),
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
        'while [ ! -e "$ATOMIC_INSTALL_RELEASE" ]; do sleep 0.01; done\n'
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
        start_new_session=True,
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
        install_release.touch()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
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


@pytest.mark.parametrize(
    "kind",
    (
        "outside-root",
        "relative",
        "traversal",
        "newline",
        "missing",
        "symlink",
        "file",
        "invalid-generation",
        "duplicate-separator",
    ),
)
def test_should_reject_unsafe_backup_directory_before_deploy_mutation(
    tmp_path: Path,
    kind: str,
) -> None:
    environment, call_log = _prepare_deploy_scenario(tmp_path)
    backup = Path(json.loads(environment["DEPLOY_BACKUP_OUTPUT"])["backupDirectory"])
    value = str(backup)
    if kind == "outside-root":
        value = str(tmp_path / backup.name)
    elif kind == "relative":
        value = backup.name
    elif kind == "traversal":
        value = str(backup.parent) + "/../backups/" + backup.name
    elif kind == "newline":
        value += "\n"
    elif kind == "duplicate-separator":
        value = str(backup.parent) + "//" + backup.name
    elif kind == "invalid-generation":
        value = str(backup.parent / "manual-directory")
        Path(value).mkdir()
    else:
        backup.rmdir()
        if kind == "symlink":
            target = tmp_path / "other-directory"
            target.mkdir()
            backup.symlink_to(target, target_is_directory=True)
        elif kind == "file":
            backup.write_text("not a directory")
    environment["DEPLOY_BACKUP_OUTPUT"] = json.dumps(
        {"status": "ok", "backupDirectory": value}
    )
    revision = tmp_path / "config" / "dogfood.revision"
    original_revision = revision.read_bytes()

    result = subprocess.run(
        command_with_root_owned_revision(
            revision,
            [
                str(DOGFOOD_SCRIPTS_DIR / "deploy.sh"),
                "--commit",
                NEXT_REVISION,
            ],
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    calls = call_log.read_text()
    assert "backup-verify" not in calls
    assert "manifest-write" not in calls
    assert "checkout --detach" not in calls
    assert "revision-update" not in calls
    assert "restart" not in calls
    assert revision.read_bytes() == original_revision
    assert not tuple((tmp_path / "state" / "deployments").glob("*.json"))

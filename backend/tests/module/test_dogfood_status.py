from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.dogfood_infrastructure_test_support import (
    TEST_REVISION,
    TEST_SECRET_SENTINEL,
    command_with_root_owned_revision,
    write_dogfood_env,
)
from tests.environment_entrypoint_test_support import ROOT_DIR, write_executable


DOGFOOD_SCRIPTS_DIR = ROOT_DIR / "scripts" / "dogfood"
READ_ONLY_COMMANDS = {
    "systemctl",
    "ss",
    "ps",
    "free",
    "nvidia-smi",
    "docker",
    "git",
    "readiness",
}
FORBIDDEN_STATUS_COMMANDS = {"curl", "wget", "journalctl", "nc", "lsof"}
CONVERSATION_SENTINEL = "会話本文をstatusへ出力してはならない"
PROMPT_SENTINEL = "promptをstatusへ出力してはならない"


def _recording_command(bin_dir: Path, name: str, log_path: Path, output: str) -> None:
    write_executable(
        bin_dir / name,
        f'printf "%s\\t%s\\n" "{name}" "$*" >> "{log_path}"\n'
        f"printf '%s\\n' {output!r}\n",
    )


def _run_status(
    tmp_path: Path,
    *,
    environment_id: str = "dogfood",
    wsl_distribution: str = "Ubuntu-dogfood",
    gpu_exit_code: int = 0,
    readiness_exit_code: int = 0,
    orchestrator_state: str = "alive",
) -> tuple[subprocess.CompletedProcess[str], list[tuple[str, str]], Path, Path, Path]:
    runtime_dir = tmp_path / "status-runtime"
    runtime_dir.mkdir()
    for script_name in ("status.sh", "load-environment.sh"):
        shutil.copy2(DOGFOOD_SCRIPTS_DIR / script_name, runtime_dir / script_name)
    resolver_log = tmp_path / "resolver.log"
    write_executable(
        runtime_dir / "resolve-inference-endpoints.py",
        f'printf "resolver\\n" >> "{resolver_log}"\n'
        "printf '%s\\n' OLLAMA_PORT=11434 VOICEVOX_PORT=50021 "
        "WHISPER_PORT=50022 LIVEKIT_PORT=17880\n",
    )
    status_script = runtime_dir / "status.sh"
    env_path, data_dir = write_dogfood_env(tmp_path)
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "DS_ENVIRONMENT_ID=dogfood",
            f"DS_ENVIRONMENT_ID={environment_id}",
        ),
        encoding="utf-8",
    )
    (data_dir / "conversation-history.db").write_text(
        CONVERSATION_SENTINEL,
        encoding="utf-8",
    )
    (data_dir / "private-prompt").write_text(PROMPT_SENTINEL, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "commands.log"
    outputs = {
        "systemctl": "ActiveState=active\\nActiveEnterTimestamp=Fri 2026-08-14 12:00:00 JST",
        "ss": "LISTEN 0 4096 127.0.0.1:12345",
        "ps": "PID %CPU %MEM COMMAND",
        "free": "Mem: 1024 512 512",
        "docker": "digital-souls-voicevox running",
    }
    for name, output in outputs.items():
        _recording_command(bin_dir, name, log_path, output)
    _recording_command(bin_dir, "git", log_path, TEST_REVISION)
    clone_cli = tmp_path / "clone" / "environments" / "environment_cli.py"
    clone_cli.parent.mkdir(parents=True)
    write_executable(
        clone_cli,
        f'printf "readiness\\t%s\\n" "$*" >> "{log_path}"\n'
        f"printf '%s\\n' 'orchestrator state={orchestrator_state}'\n"
        "printf '%s\\n' "
        '\'{"status":"ready","profile":"dogfood","services":{}}\'\n'
        f"exit {readiness_exit_code}\n",
    )
    venv_python = tmp_path / "clone" / "backend" / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    write_executable(
        venv_python,
        'exec "$@"\n',
    )
    for name in FORBIDDEN_STATUS_COMMANDS:
        _recording_command(bin_dir, name, log_path, "forbidden")
    write_executable(
        bin_dir / "nvidia-smi",
        f'printf "%s\\t%s\\n" "nvidia-smi" "$*" >> "{log_path}"\n'
        f"exit {gpu_exit_code}\n",
    )

    result = subprocess.run(
        command_with_root_owned_revision(
            tmp_path / "config" / "dogfood.revision", [str(status_script)]
        ),
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DOGFOOD_ENV_FILE": str(env_path),
            "WSL_DISTRO_NAME": wsl_distribution,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = (
        [
            tuple(line.split("\t", maxsplit=1))
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]
        if log_path.exists()
        else []
    )
    return result, calls, data_dir, resolver_log, log_path


def test_should_report_active_unit_with_dead_orchestrator_as_inconsistent(
    tmp_path: Path,
) -> None:
    result, calls, _, _, _ = _run_status(tmp_path, orchestrator_state="dead")

    assert result.returncode != 0
    assert any(
        name == "systemctl" and "digital-souls-application.service" in arguments
        for name, arguments in calls
    )
    assert "restart-services.sh" in result.stderr


def test_should_report_only_runtime_metadata_with_read_only_commands(
    tmp_path: Path,
) -> None:
    result, calls, data_dir, resolver_log, _ = _run_status(tmp_path)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert {name for name, _ in calls} == READ_ONLY_COMMANDS
    assert all(
        arguments.startswith("show ")
        for name, arguments in calls
        if name == "systemctl"
    )
    assert all(
        arguments.startswith("ps ") for name, arguments in calls if name == "docker"
    )
    assert "dogfood" in result.stdout
    assert str(data_dir) in result.stdout
    assert TEST_REVISION in result.stdout
    assert "Fri 2026-08-14 12:00:00 JST" in result.stdout
    assert '"status":"ready"' in result.stdout.replace(" ", "")
    ss_arguments = " ".join(arguments for name, arguments in calls if name == "ss")
    assert "11434" in ss_arguments
    assert "50021" in ss_arguments
    assert "50022" in ss_arguments
    assert "17880" in ss_arguments
    systemctl_arguments = " ".join(
        arguments for name, arguments in calls if name == "systemctl"
    )
    docker_arguments = " ".join(arguments for name, arguments in calls if name == "docker")
    assert "digital-souls-livekit.service" in systemctl_arguments
    assert "digital-souls-livekit" in docker_arguments
    assert resolver_log.read_text(encoding="utf-8") == "resolver\n"
    readiness_calls = [arguments for name, arguments in calls if name == "readiness"]
    assert readiness_calls == ["readiness --profile dogfood"]


def test_should_not_expose_private_content_in_status_output(tmp_path: Path) -> None:
    result, _, _, _, _ = _run_status(tmp_path)

    assert result.returncode == 0, (result.stdout, result.stderr)
    for observation in (result.stdout, result.stderr):
        for sentinel in (
            TEST_SECRET_SENTINEL,
            CONVERSATION_SENTINEL,
            PROMPT_SENTINEL,
        ):
            assert sentinel not in observation


def test_should_keep_status_available_when_gpu_metadata_command_is_unavailable(
    tmp_path: Path,
) -> None:
    result, calls, _, _, _ = _run_status(tmp_path, gpu_exit_code=127)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert any(name == "nvidia-smi" for name, _ in calls)
    assert "GPU" in result.stdout


def test_should_report_all_metadata_before_returning_not_ready(
    tmp_path: Path,
) -> None:
    result, calls, data_dir, _, _ = _run_status(tmp_path, readiness_exit_code=1)

    assert result.returncode == 1
    assert str(data_dir) in result.stdout
    assert TEST_REVISION in result.stdout
    assert {"ss", "ps", "free", "nvidia-smi", "docker"} <= {
        name for name, _ in calls
    }


@pytest.mark.parametrize(
    ("environment_id", "wsl_distribution"),
    (("development", "Ubuntu-dogfood"), ("dogfood", "Ubuntu-dev")),
)
def test_should_stop_before_metadata_commands_when_identity_does_not_match(
    tmp_path: Path,
    environment_id: str,
    wsl_distribution: str,
) -> None:
    result, calls, _, resolver_log, command_log = _run_status(
        tmp_path,
        environment_id=environment_id,
        wsl_distribution=wsl_distribution,
    )

    assert result.returncode != 0
    assert calls == []
    assert not resolver_log.exists()
    assert not command_log.exists()

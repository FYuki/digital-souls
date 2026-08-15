from __future__ import annotations

from pathlib import Path

import pytest

from tests.environment_entrypoint_test_support import (
    ROOT_DIR,
    run_with_recording_python as _run_with_recording_python,
)
@pytest.mark.parametrize(
    ("wrapper_name", "expected_profile"),
    [
        ("start-all.sh", "dev"),
        ("start-voice-chat-e2e.sh", "integration-voice"),
    ],
)
def test_should_delegate_wrapper_arguments_and_default_profile_to_common_up(
    wrapper_name: str,
    expected_profile: str,
    tmp_path: Path,
):
    wrapper = ROOT_DIR / "scripts" / wrapper_name

    process, stdout, stderr, arguments, child_pid = _run_with_recording_python(
        wrapper,
        ["--run-report", str(tmp_path / "run.json")],
        tmp_path,
        23,
    )

    assert process.returncode == 23, (stdout, stderr)
    assert child_pid == process.pid
    assert arguments[1:] == [
        "start",
        "--default-profile",
        expected_profile,
        "--run-report",
        str(tmp_path / "run.json"),
    ]

@pytest.mark.parametrize(
    ("script_name", "command"),
    [("up.sh", "start"), ("down.sh", "down"), ("verify.sh", "verify")],
)
def test_should_route_environment_shell_entrypoint_to_python_cli(
    script_name: str,
    command: str,
    tmp_path: Path,
):
    entrypoint = ROOT_DIR / "environments" / script_name

    process, stdout, stderr, arguments, child_pid = _run_with_recording_python(
        entrypoint, ["--example", "value"], tmp_path, 31
    )

    assert process.returncode == 31, (stdout, stderr)
    assert child_pid == process.pid
    assert arguments[-3:] == [
        command,
        "--example",
        "value",
    ]

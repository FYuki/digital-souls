from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import subprocess

import pytest

from app.inference.adapters.openai_codex import OpenAICodexAdapter
from app.inference.contracts import (
    InferenceMessage,
    TextGenerationRequest,
    TokenEstimateAccuracy,
    TokenEstimateRequest,
)
from app.inference.errors import InferenceError, InferenceErrorCategory


_HELP = " ".join(
    (
        "--ephemeral",
        "--ignore-rules",
        "--ignore-user-config",
        "--json",
        "--sandbox",
        "--skip-git-repo-check",
        "--strict-config",
    )
)
_FEATURES = "\n".join(
    (
        "apps stable true",
        "browser_use stable true",
        "browser_use_external stable true",
        "browser_use_full_cdp_access stable true",
        "code_mode_host stable true",
        "computer_use stable true",
        "hooks stable true",
        "image_generation stable true",
        "in_app_browser stable true",
        "multi_agent stable true",
        "plugins stable true",
        "shell_tool stable true",
        "skill_search stable true",
        "tool_suggest stable true",
        "unified_exec stable true",
        "view_image stable true",
    )
)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.version = "codex-cli 0.152.0\n"
        self.help = _HELP
        self.features = _FEATURES
        self.exec_stdout = (
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"reply"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":1}}\n'
        )
        self.exec_stderr = ""
        self.exec_returncode = 0
        self.login_returncode = 0
        self.login_stdout = "Logged in using ChatGPT"
        self.timeout_on_exec = False

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        input_text: str | None,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(
            {
                "arguments": arguments,
                "input_text": input_text,
                "cwd": cwd,
                "cwd_entries": tuple(cwd.iterdir()),
                "environment": dict(environment),
                "timeout_seconds": timeout_seconds,
            }
        )
        if arguments[-1:] == ("--version",):
            return subprocess.CompletedProcess(arguments, 0, self.version, "")
        if arguments[-2:] == ("exec", "--help"):
            return subprocess.CompletedProcess(arguments, 0, self.help, "")
        if arguments[-2:] == ("features", "list"):
            return subprocess.CompletedProcess(arguments, 0, self.features, "")
        if arguments[-2:] == ("login", "status"):
            return subprocess.CompletedProcess(
                arguments,
                self.login_returncode,
                self.login_stdout if self.login_returncode == 0 else "",
                "not logged in" if self.login_returncode else "",
            )
        if self.timeout_on_exec:
            raise subprocess.TimeoutExpired(arguments, timeout_seconds)
        return subprocess.CompletedProcess(
            arguments,
            self.exec_returncode,
            self.exec_stdout,
            self.exec_stderr,
        )


def _adapter(tmp_path: Path, runner: RecordingRunner) -> OpenAICodexAdapter:
    codex_home = tmp_path / "codex-auth"
    codex_home.mkdir()
    return OpenAICodexAdapter(
        executable=Path("/usr/bin/codex"),
        codex_home=codex_home,
        inherited_environment={
            "PATH": "/usr/bin",
            "LANG": "C.UTF-8",
            "OPENAI_API_KEY": "must-not-be-inherited",
            "ANOTHER_SECRET": "must-not-be-inherited",
        },
        runner=runner,
    )


def _request() -> TextGenerationRequest:
    return TextGenerationRequest(
        messages=(
            InferenceMessage("system", "system"),
            InferenceMessage("user", "hello"),
        ),
        model_id="gpt-5.6-sol",
        options={"reasoning_effort": "high"},
        max_input_tokens=8_192,
        max_output_tokens=1_024,
        timeout_seconds=5.0,
    )


def test_probe_validates_runtime_and_subscription_without_inference(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    adapter = _adapter(tmp_path, runner)

    adapter.probe("gpt-5.6-sol", timeout_seconds=2.0)

    arguments = [call["arguments"] for call in runner.calls]
    assert arguments == [
        ("/usr/bin/codex", "--version"),
        ("/usr/bin/codex", "exec", "--help"),
        ("/usr/bin/codex", "features", "list"),
        ("/usr/bin/codex", "login", "status"),
    ]
    assert all(call["input_text"] is None for call in runner.calls)


def test_generate_text_enforces_stateless_tool_free_isolation(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    adapter = _adapter(tmp_path, runner)

    result = adapter.generate_text(_request())

    assert result.text == "reply"
    assert len(runner.calls) == 5
    execution = runner.calls[-1]
    arguments = execution["arguments"]
    assert isinstance(arguments, tuple)
    for required in (
        "--ask-for-approval",
        "never",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--json",
        "read-only",
        "shell_tool",
        'web_search="disabled"',
    ):
        assert required in arguments
    assert execution["cwd_entries"] == ()
    cwd = execution["cwd"]
    assert isinstance(cwd, Path) and not cwd.exists()
    environment = execution["environment"]
    assert isinstance(environment, dict)
    assert set(environment) == {"CODEX_HOME", "HOME", "LANG", "PATH"}
    assert "must-not-be-inherited" not in str(environment)
    assert "hello" not in str(arguments)
    assert "hello" in str(execution["input_text"])


def test_runtime_validation_is_cached_across_requests(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = _adapter(tmp_path, runner)

    adapter.generate_text(_request())
    adapter.generate_text(_request())

    assert len(runner.calls) == 6


@pytest.mark.parametrize(
    ("version", "help_text"),
    [
        ("codex-cli 0.151.0\n", _HELP),
        ("codex-cli 0.152.0\n", _HELP.replace("--ignore-user-config", "")),
        ("unknown\n", _HELP),
    ],
)
def test_runtime_without_required_isolation_contract_is_rejected(
    tmp_path: Path,
    version: str,
    help_text: str,
) -> None:
    runner = RecordingRunner()
    runner.version = version
    runner.help = help_text

    with pytest.raises(InferenceError) as exc_info:
        _adapter(tmp_path, runner).generate_text(_request())

    assert exc_info.value.category is InferenceErrorCategory.UNSUPPORTED_CAPABILITY
    assert len(runner.calls) == 3


def test_tool_event_is_rejected_even_if_runtime_emits_one(tmp_path: Path) -> None:
    runner = RecordingRunner()
    runner.exec_stdout = (
        '{"type":"item.completed","item":{"type":"command_execution"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"unsafe"}}\n'
    )

    with pytest.raises(InferenceError) as exc_info:
        _adapter(tmp_path, runner).generate_text(_request())

    assert exc_info.value.category is InferenceErrorCategory.ACCESS_DENIED


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        '{"type":"item.completed","item":{"type":"agent_message"}}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":""}}\n',
    ],
)
def test_malformed_runtime_output_is_rejected_without_payload(
    tmp_path: Path,
    stdout: str,
) -> None:
    runner = RecordingRunner()
    runner.exec_stdout = stdout

    with pytest.raises(InferenceError) as exc_info:
        _adapter(tmp_path, runner).generate_text(_request())

    assert exc_info.value.category is InferenceErrorCategory.INVALID_RESPONSE
    assert "not-json" not in str(exc_info.value)


def test_timeout_terminates_as_common_retryable_error(tmp_path: Path) -> None:
    runner = RecordingRunner()
    runner.timeout_on_exec = True

    with pytest.raises(InferenceError) as exc_info:
        _adapter(tmp_path, runner).generate_text(_request())

    assert exc_info.value.category is InferenceErrorCategory.TIMEOUT
    assert exc_info.value.retryable is True


@pytest.mark.parametrize(
    ("stderr", "category", "retryable"),
    [
        (
            "not logged in private-value",
            InferenceErrorCategory.AUTHENTICATION_FAILED,
            False,
        ),
        (
            "model not found private-value",
            InferenceErrorCategory.MODEL_NOT_FOUND,
            False,
        ),
        (
            "permission denied private-value",
            InferenceErrorCategory.PERMISSION_DENIED,
            False,
        ),
        ("rate limit private-value", InferenceErrorCategory.RATE_LIMITED, True),
        ("unclassified private-value", InferenceErrorCategory.PROVIDER_ERROR, False),
    ],
)
def test_process_failures_are_normalized_without_raw_stderr(
    tmp_path: Path,
    stderr: str,
    category: InferenceErrorCategory,
    retryable: bool,
) -> None:
    runner = RecordingRunner()
    runner.exec_returncode = 1
    runner.exec_stderr = stderr

    with pytest.raises(InferenceError) as exc_info:
        _adapter(tmp_path, runner).generate_text(_request())

    assert exc_info.value.category is category
    assert exc_info.value.retryable is retryable
    assert "private-value" not in str(exc_info.value)


def test_login_status_uses_runtime_without_reading_auth_cache(tmp_path: Path) -> None:
    runner = RecordingRunner()
    runner.login_returncode = 1

    with pytest.raises(InferenceError) as exc_info:
        _adapter(tmp_path, runner).login_status(timeout_seconds=2.0)

    assert exc_info.value.category is InferenceErrorCategory.AUTHENTICATION_FAILED
    assert runner.calls[-1]["arguments"] == (
        "/usr/bin/codex",
        "login",
        "status",
    )


def test_codex_api_key_login_is_not_accepted_as_subscription(tmp_path: Path) -> None:
    runner = RecordingRunner()
    runner.login_stdout = "Logged in using an API key"

    with pytest.raises(InferenceError) as exc_info:
        _adapter(tmp_path, runner).generate_text(_request())

    assert exc_info.value.category is InferenceErrorCategory.AUTHENTICATION_FAILED


def test_token_estimate_is_local_and_does_not_start_codex(tmp_path: Path) -> None:
    runner = RecordingRunner()
    adapter = _adapter(tmp_path, runner)

    estimate = adapter.estimate_input_tokens(
        TokenEstimateRequest(
            messages=(InferenceMessage("user", "hello"),),
            model_id="gpt-5.6-sol",
            options={},
            max_input_tokens=8_192,
            timeout_seconds=2.0,
        )
    )

    assert estimate.count > 0
    assert estimate.accuracy is TokenEstimateAccuracy.ESTIMATED
    assert runner.calls == []

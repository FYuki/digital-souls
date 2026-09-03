from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Protocol

from app.inference.contracts import (
    EmbeddingRequest,
    EmbeddingResult,
    InferenceCapability,
    InferenceMessage,
    JsonValue,
    ProviderTextResult,
    StructuredGenerationRequest,
    TextGenerationRequest,
    TokenEstimate,
    TokenEstimateAccuracy,
    TokenEstimateRequest,
)
from app.inference.errors import InferenceError, InferenceErrorCategory


_MINIMUM_CODEX_VERSION = (0, 152, 0)
_VERSION_PATTERN = re.compile(r"codex-cli (\d+)\.(\d+)\.(\d+)\Z")
_REQUIRED_EXEC_FLAGS = frozenset(
    {
        "--ephemeral",
        "--ignore-rules",
        "--ignore-user-config",
        "--json",
        "--sandbox",
        "--skip-git-repo-check",
        "--strict-config",
    }
)
_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode_host",
    "computer_use",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugins",
    "shell_tool",
    "skill_search",
    "tool_suggest",
    "unified_exec",
    "view_image",
)
_SAFE_INHERITED_ENVIRONMENT = frozenset(
    {"LANG", "LC_ALL", "PATH", "SSL_CERT_DIR", "SSL_CERT_FILE", "TZ"}
)
_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "computer_tool_call",
        "file_change",
        "mcp_tool_call",
        "tool_call",
        "web_search",
    }
)


class CodexProcessRunner(Protocol):
    def run(
        self,
        arguments: tuple[str, ...],
        *,
        input_text: str | None,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessCodexRunner:
    def run(
        self,
        arguments: tuple[str, ...],
        *,
        input_text: str | None,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            arguments,
            input=input_text,
            cwd=cwd,
            env=dict(environment),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )


class OpenAICodexAdapter:
    """ChatGPT認証を公式Codex runtimeへ委譲するone-shot text Adapter。"""

    provider_id = "openai-codex"
    capabilities = frozenset(
        {
            InferenceCapability.GENERATE_TEXT,
            InferenceCapability.ESTIMATE_INPUT_TOKENS,
        }
    )

    def __init__(
        self,
        *,
        executable: Path,
        codex_home: Path,
        inherited_environment: Mapping[str, str],
        runner: CodexProcessRunner | None = None,
    ) -> None:
        if not executable.is_absolute():
            raise ValueError("Codex executable must be an absolute path")
        if not codex_home.is_absolute():
            raise ValueError("Codex credential directory must be an absolute path")
        self._executable = executable
        self._codex_home = codex_home
        self._environment = self._isolated_environment(inherited_environment)
        self._runner = runner or SubprocessCodexRunner()
        self._runtime_validated = False
        self._subscription_validated = False

    def close(self) -> None:
        return None

    def generate_text(self, request: TextGenerationRequest) -> ProviderTextResult:
        self.login_status(timeout_seconds=min(request.timeout_seconds, 10.0))
        prompt = self._prompt(request.messages)
        arguments = self._execution_arguments(request.model_id, request.options)
        try:
            with tempfile.TemporaryDirectory(
                prefix="digital-souls-codex-"
            ) as directory:
                result = self._runner.run(
                    arguments,
                    input_text=prompt,
                    cwd=Path(directory),
                    environment=self._environment,
                    timeout_seconds=request.timeout_seconds,
                )
        except subprocess.TimeoutExpired:
            raise InferenceError(
                InferenceErrorCategory.TIMEOUT,
                retryable=True,
            ) from None
        except OSError:
            raise InferenceError(
                InferenceErrorCategory.UNAVAILABLE,
                retryable=True,
            ) from None
        if result.returncode != 0:
            self._raise_process_failure(result.stderr)
        return ProviderTextResult(text=self._final_text(result.stdout), usage=None)

    async def stream_text(self, request: TextGenerationRequest) -> AsyncIterator[str]:
        del request
        raise InferenceError(
            InferenceErrorCategory.UNSUPPORTED_CAPABILITY,
            retryable=False,
        )
        yield ""  # pragma: no cover

    def generate_structured(
        self, request: StructuredGenerationRequest
    ) -> ProviderTextResult:
        del request
        raise InferenceError(
            InferenceErrorCategory.UNSUPPORTED_CAPABILITY,
            retryable=False,
        )

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        del request
        raise InferenceError(
            InferenceErrorCategory.UNSUPPORTED_CAPABILITY,
            retryable=False,
        )

    def estimate_input_tokens(self, request: TokenEstimateRequest) -> TokenEstimate:
        serialized: dict[str, object] = {
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "model": request.model_id,
            "options": dict(request.options),
        }
        byte_count = len(
            json.dumps(
                serialized,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return TokenEstimate(
            count=max(1, math.ceil(byte_count / 3 * 1.2)),
            accuracy=TokenEstimateAccuracy.ESTIMATED,
            method="codex_prompt_utf8_div3_margin20pct",
        )

    def validate_runtime(self, *, timeout_seconds: float) -> None:
        if self._runtime_validated:
            return
        try:
            with tempfile.TemporaryDirectory(
                prefix="digital-souls-codex-probe-"
            ) as directory:
                cwd = Path(directory)
                version = self._runner.run(
                    (str(self._executable), "--version"),
                    input_text=None,
                    cwd=cwd,
                    environment=self._environment,
                    timeout_seconds=timeout_seconds,
                )
                help_result = self._runner.run(
                    (str(self._executable), "exec", "--help"),
                    input_text=None,
                    cwd=cwd,
                    environment=self._environment,
                    timeout_seconds=timeout_seconds,
                )
                feature_result = self._runner.run(
                    (str(self._executable), "features", "list"),
                    input_text=None,
                    cwd=cwd,
                    environment=self._environment,
                    timeout_seconds=timeout_seconds,
                )
        except (OSError, subprocess.TimeoutExpired):
            raise InferenceError(
                InferenceErrorCategory.UNAVAILABLE,
                retryable=True,
            ) from None
        if (
            version.returncode != 0
            or help_result.returncode != 0
            or feature_result.returncode != 0
        ):
            raise InferenceError(
                InferenceErrorCategory.UNAVAILABLE,
                retryable=False,
            )
        match = _VERSION_PATTERN.fullmatch(version.stdout.strip())
        if match is None or tuple(map(int, match.groups())) < _MINIMUM_CODEX_VERSION:
            raise InferenceError(
                InferenceErrorCategory.UNSUPPORTED_CAPABILITY,
                retryable=False,
            )
        missing_flags = _REQUIRED_EXEC_FLAGS - set(help_result.stdout.split())
        available_features = {
            line.split(maxsplit=1)[0]
            for line in feature_result.stdout.splitlines()
            if line.strip()
        }
        if missing_flags or set(_DISABLED_FEATURES) - available_features:
            raise InferenceError(
                InferenceErrorCategory.UNSUPPORTED_CAPABILITY,
                retryable=False,
            )
        self._runtime_validated = True

    def login_status(self, *, timeout_seconds: float) -> None:
        if self._subscription_validated:
            return
        self.validate_runtime(timeout_seconds=timeout_seconds)
        try:
            with tempfile.TemporaryDirectory(
                prefix="digital-souls-codex-login-"
            ) as directory:
                result = self._runner.run(
                    (str(self._executable), "login", "status"),
                    input_text=None,
                    cwd=Path(directory),
                    environment=self._environment,
                    timeout_seconds=timeout_seconds,
                )
        except (OSError, subprocess.TimeoutExpired):
            raise InferenceError(
                InferenceErrorCategory.UNAVAILABLE,
                retryable=True,
            ) from None
        if result.returncode != 0 or "chatgpt" not in result.stdout.lower():
            raise InferenceError(
                InferenceErrorCategory.AUTHENTICATION_FAILED,
                retryable=False,
            )
        self._subscription_validated = True

    def _execution_arguments(
        self,
        model_id: str,
        options: Mapping[str, JsonValue],
    ) -> tuple[str, ...]:
        arguments = [
            str(self._executable),
            "--ask-for-approval",
            "never",
            "--config",
            'web_search="disabled"',
            "--config",
            'shell_environment_policy.inherit="none"',
            "--config",
            "shell_environment_policy.experimental_use_profile=false",
            "exec",
            "-",
            "--model",
            model_id,
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--json",
            "--color",
            "never",
        ]
        for feature in _DISABLED_FEATURES:
            arguments.extend(("--disable", feature))
        reasoning_effort = options.get("reasoning_effort")
        if reasoning_effort is not None:
            arguments.extend(
                ("--config", f'model_reasoning_effort="{reasoning_effort}"')
            )
        return tuple(arguments)

    def _isolated_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        isolated = {
            key: value
            for key, value in environment.items()
            if key in _SAFE_INHERITED_ENVIRONMENT
        }
        isolated["CODEX_HOME"] = str(self._codex_home)
        isolated["HOME"] = str(self._codex_home)
        return isolated

    @staticmethod
    def _prompt(messages: tuple[InferenceMessage, ...]) -> str:
        return json.dumps(
            {
                "task": "Return only the final text response to the following messages.",
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in messages
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _final_text(stdout: str) -> str:
        final_text: str | None = None
        try:
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                event: object = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError
                item = event.get("item")
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type in _TOOL_ITEM_TYPES:
                    raise InferenceError(
                        InferenceErrorCategory.ACCESS_DENIED,
                        retryable=False,
                    )
                if (
                    event.get("type") == "item.completed"
                    and item_type == "agent_message"
                    and isinstance(item.get("text"), str)
                ):
                    final_text = item["text"]
        except (json.JSONDecodeError, ValueError):
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            ) from None
        if final_text is None or not final_text:
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        return final_text

    @staticmethod
    def _raise_process_failure(stderr: str) -> None:
        normalized = stderr.lower()
        if any(
            token in normalized
            for token in ("not logged in", "login required", "authentication")
        ):
            category = InferenceErrorCategory.AUTHENTICATION_FAILED
            retryable = False
        elif any(
            token in normalized for token in ("model not found", "unsupported model")
        ):
            category = InferenceErrorCategory.MODEL_NOT_FOUND
            retryable = False
        elif any(
            token in normalized for token in ("permission denied", "access denied")
        ):
            category = InferenceErrorCategory.PERMISSION_DENIED
            retryable = False
        elif any(token in normalized for token in ("rate limit", "too many requests")):
            category = InferenceErrorCategory.RATE_LIMITED
            retryable = True
        else:
            category = InferenceErrorCategory.PROVIDER_ERROR
            retryable = False
        raise InferenceError(category, retryable=retryable)

"""Evaluation-only Codex subscription Provider; credentials belong to the CLI."""

import asyncio
import json
import os
import re
import subprocess
import tempfile
from collections.abc import AsyncGenerator
from typing import Any

from .application import CoreError
from .character import Profile
from .semantic_answer_external import ExternalAnswerProfile

MINIMUM_VERSION = (0, 152, 0)
REQUIRED_EXEC_FLAGS = frozenset(
    {
        "--ephemeral",
        "--ignore-rules",
        "--ignore-user-config",
        "--json",
        "--sandbox",
        "--skip-git-repo-check",
        "--strict-config",
        "--model",
        "--color",
        "--cd",
        "--config",
        "--disable",
    }
)
DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode_host",
    "computer_use",
    "fast_mode",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "in_app_local_automation",
    "multi_agent",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "sleep_tool",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "ultrafast_mode",
    "unified_exec",
    "unified_exec_tty",
    "view_image",
    "workspace_dependencies",
    "worktrees",
    "write_stdin_approval",
)
SAFE_ENVIRONMENT = frozenset({"PATH", "LANG", "LC_ALL", "TZ"})
TOOL_ITEMS = frozenset(
    {
        "command_execution",
        "computer_tool_call",
        "file_change",
        "mcp_tool_call",
        "tool_call",
        "web_search",
    }
)
CONDITION_NOTE = "codex-cli-adds-own-instructions-not-pure-api"


class CodexSubscriptionAnswerProvider:
    def __init__(
        self, profile: ExternalAnswerProfile, *, timeout_seconds: float | None = None
    ) -> None:
        profile.require_enabled()
        timeout_seconds = (
            profile.chat.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if profile.answer_provider != "codex_subscription" or timeout_seconds <= 0:
            raise ValueError("Codex subscription profile required")
        self.profile = profile
        self.timeout_seconds = timeout_seconds
        self.cli_version: str | None = None

    def _run(self, argv: list[str], directory: str, prompt: str | None = None) -> str:
        environment = {k: v for k, v in os.environ.items() if k in SAFE_ENVIRONMENT}
        # Pass paths only. Never inspect, copy or probe authentication files.
        assert self.profile.codex_home is not None
        environment.update(HOME=self.profile.codex_home, CODEX_HOME=self.profile.codex_home)
        result = subprocess.run(
            [str(self.profile.codex_executable), *argv],
            cwd=directory,
            env=environment,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if result.returncode:
            raise ValueError
        return result.stdout

    def _complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if profile != self.profile.chat or set(payload) - {"messages", "stream"}:
                raise ValueError
            if payload.get("stream", False) is not False:
                raise ValueError
            messages = payload.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError
            for message in messages:
                if (
                    not isinstance(message, dict)
                    or set(message) != {"role", "content"}
                    or message["role"] not in {"system", "user", "assistant"}
                    or type(message["content"]) is not str
                ):
                    raise ValueError
            prompt = json.dumps(
                {
                    "task": "Return only the final text response to the following messages.",
                    "messages": messages,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            with tempfile.TemporaryDirectory(prefix="core-codex-answer-") as directory:
                version = self._run(["--version"], directory).strip()
                match = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)", version)
                if match is None or tuple(map(int, match.groups())) < MINIMUM_VERSION:
                    raise ValueError
                help_text = self._run(["exec", "--help"], directory)
                if REQUIRED_EXEC_FLAGS - set(help_text.split()):
                    raise ValueError
                self.cli_version = ".".join(match.groups())
                argv = [
                    "--ask-for-approval",
                    "never",
                    "-c",
                    'web_search="disabled"',
                    "-c",
                    'shell_environment_policy.inherit="none"',
                    "-c",
                    "shell_environment_policy.experimental_use_profile=false",
                    "-c",
                    "mcp_servers={}",
                    "-c",
                    'model_provider="openai"',
                    "exec",
                    "-",
                    "--model",
                    profile.model.split("/", 1)[1],
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
                    "-C",
                    directory,
                ]
                for feature in DISABLED_FEATURES:
                    argv.extend(("--disable", feature))
                text = self._final_text(self._run(argv, directory, prompt))
            return {"choices": [{"message": {"role": "assistant", "content": text}}]}
        except Exception:
            raise CoreError(502, "provider_error", "Codex answer provider failed") from None

    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._complete, profile, payload)

    async def stream(
        self, profile: Profile, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        raise CoreError(502, "provider_error", "Codex answer provider failed")
        yield  # pragma: no cover

    @staticmethod
    def _final_text(stdout: str) -> str:
        final: str | None = None
        for line in stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("type") in {"error", "turn.failed"}:
                raise ValueError
            item = event.get("item")
            if item is None:
                continue
            if not isinstance(item, dict) or item.get("type") in TOOL_ITEMS:
                raise ValueError
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                if type(item.get("text")) is not str:
                    raise ValueError
                final = item["text"]
        if not final:
            raise ValueError
        return final

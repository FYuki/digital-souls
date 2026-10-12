"""Codex subscription adapter contracts, using only synthetic child processes."""

import json
from pathlib import Path
from typing import Any

import pytest

from digital_souls_core.application import CoreError
from digital_souls_core.semantic_answer_codex import CodexSubscriptionAnswerProvider
from digital_souls_core.semantic_answer_external import ExternalAnswerProfile, load_external_profile
from digital_souls_core.semantic_answer_runtime import runtime_identity

from .codex_answer_support import calls, fake_codex

PAYLOAD = {
    "messages": [
        {"role": "system", "content": "Synthetic instructions"},
        {"role": "user", "content": "Synthetic query"},
    ]
}


@pytest.mark.ut
@pytest.mark.parametrize(
    "fault", ["old", "version", "flag", "exit", "json", "missing", "tool", "failed", "timeout"]
)
async def test_codex_failures_are_content_free(
    tmp_path: Path, fault: str, capsys: pytest.CaptureFixture[str]
) -> None:
    data, record = fake_codex(tmp_path, fault=fault)
    provider = CodexSubscriptionAnswerProvider(
        ExternalAnswerProfile.model_validate(data), timeout_seconds=0.2
    )
    with pytest.raises(CoreError) as caught:
        await provider.complete(provider.profile.chat, PAYLOAD)
    assert caught.value.code == "provider_error"
    assert str(caught.value) == "Codex answer provider failed"
    assert capsys.readouterr() == ("", "")
    assert len(calls(record)) <= 3


@pytest.mark.ut
@pytest.mark.parametrize(
    "fault", ["relative-executable", "relative-home", "disabled", "provider", "api-key"]
)
def test_codex_profile_fail_closed(tmp_path: Path, fault: str) -> None:
    data, _ = fake_codex(tmp_path)
    if fault == "relative-executable":
        data["codex_executable"] = "codex"
    if fault == "relative-home":
        data["codex_home"] = "fake-home"
    if fault == "disabled":
        data["enabled"] = False
    if fault == "provider":
        data["answer_provider"] = "unknown"
    if fault == "api-key":
        data["api_key_env"] = "OPENAI_API_KEY"
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Invalid external answer profile"):
        load_external_profile(str(path))


@pytest.mark.it1
async def test_codex_argv_environment_payload_and_final_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data, record = fake_codex(tmp_path)
    for name in [
        "OPENAI_API_KEY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NODE_OPTIONS",
        "PYTHONPATH",
        "T3_ACP_MCP_NODE",
    ]:
        monkeypatch.setenv(name, "environment-sentinel")
    profile = ExternalAnswerProfile.model_validate(data)
    provider = CodexSubscriptionAnswerProvider(profile)
    assert provider.timeout_seconds == profile.chat.timeout_seconds
    original_open = Path.open

    def guarded_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        assert not self.is_relative_to(Path(str(data["codex_home"])))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = await provider.complete(profile.chat, PAYLOAD)
    assert result == {
        "choices": [{"message": {"role": "assistant", "content": "Synthetic final answer"}}]
    }
    assert provider.cli_version == "0.162.1"
    invocations = calls(record)
    assert [c["argv"] for c in invocations[:2]] == [["--version"], ["exec", "--help"]]
    call = invocations[-1]
    argv = call["argv"]
    for flag in [
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--json",
        "--skip-git-repo-check",
    ]:
        assert flag in argv
    for flag, value in [
        ("--sandbox", "read-only"),
        ("--model", "gpt-6-luna"),
        ("--color", "never"),
        ("--ask-for-approval", "never"),
        ("-C", call["cwd"]),
    ]:
        assert argv[argv.index(flag) + 1] == value
    configs = [argv[i + 1] for i, value in enumerate(argv) if value == "-c"]
    assert configs == [
        'web_search="disabled"',
        'shell_environment_policy.inherit="none"',
        "shell_environment_policy.experimental_use_profile=false",
        "mcp_servers={}",
        'model_provider="openai"',
    ]
    disabled = [argv[i + 1] for i, value in enumerate(argv) if value == "--disable"]
    assert disabled == [
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
    ]
    assert call["empty"] and not Path(call["cwd"]).exists()
    assert json.loads(call["stdin"])["messages"] == PAYLOAD["messages"]
    env = call["env"]
    assert env["CODEX_HOME"] == env["HOME"] == data["codex_home"]
    assert set(env) <= {"PATH", "LANG", "LC_ALL", "TZ", "CODEX_HOME", "HOME", "LC_CTYPE"}
    assert "environment-sentinel" not in json.dumps(env)
    assert capsys.readouterr() == ("", "")


@pytest.mark.ut
@pytest.mark.parametrize(
    "extra",
    [
        {"tools": []},
        {"tool_choice": "none"},
        {"stream": True},
        {"temperature": 1},
        {"messages": [{"role": "tool", "content": "tool"}]},
    ],
)
async def test_codex_unsupported_payload_never_starts(
    tmp_path: Path, extra: dict[str, Any]
) -> None:
    data, record = fake_codex(tmp_path)
    provider = CodexSubscriptionAnswerProvider(ExternalAnswerProfile.model_validate(data))
    with pytest.raises(CoreError):
        await provider.complete(provider.profile.chat, {**PAYLOAD, **extra})
    assert calls(record) == []


@pytest.mark.ut
async def test_codex_stream_never_starts(tmp_path: Path) -> None:
    data, record = fake_codex(tmp_path)
    provider = CodexSubscriptionAnswerProvider(ExternalAnswerProfile.model_validate(data))
    with pytest.raises(CoreError):
        await anext(provider.stream(provider.profile.chat, PAYLOAD))
    assert calls(record) == []


@pytest.mark.ut
def test_codex_identity_and_dry_run_never_start_or_read_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, record = fake_codex(tmp_path)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data))
    original = Path.open

    def guarded_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        assert not self.is_relative_to(Path(str(data["codex_home"])))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    identity = runtime_identity("external_model", str(path))
    assert identity["provider"] == "codex_subscription"
    assert identity["model"] == "gpt-6-luna"
    assert identity["codex_cli_version"] is None
    assert identity["condition_note"] == "codex-cli-adds-own-instructions-not-pure-api"
    assert identity["external_send"] is True and identity["cache"] is False
    assert calls(record) == []
    assert str(tmp_path) not in json.dumps(identity)


@pytest.mark.it1
@pytest.mark.parametrize("denial", [None, "permission", "grant", "scanner", "classifier", "revoke"])
async def test_codex_dispatch_checks_production_policy_before_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    denial: str | None,
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace
    from typing import Any, cast

    from digital_souls_core.application import Inference
    from digital_souls_core.privacy import Permission
    from digital_souls_core.semantic_answer_runtime import complete_answer
    from digital_souls_core.semantic_evaluation_runtime import CapturingRetrieval, PreparedCase

    from .privacy_support import BINDING
    from .record_retrieval_support import setup
    from .support import character

    service, records, embedding = setup()
    retrieval = CapturingRetrieval(records, service.policy, embedding)
    events: list[str] = []
    authorize = service.policy.authorize

    async def observed_authorize(binding: Any, permission: Permission, value: object) -> bool:
        result = await authorize(binding, permission, value)
        if permission == "external":
            events.append("external-authorized" if result else "external-denied")
        return result

    monkeypatch.setattr(service.policy, "authorize", observed_authorize)
    check = Inference.check

    def observed_check(prepared: Any) -> None:
        check(prepared)
        events.append("check")

    monkeypatch.setattr(Inference, "check", staticmethod(observed_check))
    data, record = fake_codex(tmp_path)
    codex_profile = ExternalAnswerProfile.model_validate(data)
    char = character("synthetic")
    char = replace(char, config=char.config.model_copy(update={"profile": codex_profile.chat}))
    if denial == "permission":
        char = replace(
            char,
            config=char.config.model_copy(
                update={
                    "profile": char.config.profile.model_copy(
                        update={"external_send_allowed": False}
                    )
                }
            ),
        )
    if denial == "grant":
        service.policy.configure({BINDING: frozenset({"history", "local", "memory"})})
    if denial == "scanner":
        char = replace(
            char,
            card=replace(
                char.card, system_prompt="api_key=sk-synthetic-secret-12345678901234567890"
            ),
        )
    if denial == "classifier":
        classifier = service.policy.classifier
        assert classifier is not None
        original_safe = classifier.safe

        async def deny_final(value: object, version: str) -> bool:
            if isinstance(value, dict) and any(
                m.get("role") == "system" for m in value.get("messages", [])
            ):
                return False
            return await original_safe(value, version)

        monkeypatch.setattr(classifier, "safe", deny_final)

    def mutate(phase: str) -> None:
        if denial == "revoke" and phase == "after_search":
            service.policy.configure({})

    runtime = cast(
        PreparedCase,
        SimpleNamespace(
            case=SimpleNamespace(
                id="synthetic", query="tea", binding=SimpleNamespace(to_domain=lambda: BINDING)
            ),
            retrieval=retrieval,
            conversation_ids={"synthetic": "conversation"},
            mutate=mutate,
        ),
    )

    class ObservedProvider(CodexSubscriptionAnswerProvider):
        async def complete(self, profile: Any, payload: dict[str, Any]) -> dict[str, Any]:
            assert events[-1] == "check" and "external-authorized" in events
            events.append("provider")
            return await super().complete(profile, payload)

    provider = ObservedProvider(codex_profile)
    if denial in {"permission", "grant", "scanner", "classifier"}:
        with pytest.raises(ValueError, match="Answer evaluation failed"):
            await complete_answer(runtime, char, provider)
    else:
        observation = await complete_answer(runtime, char, provider)
        assert observation["dispatched"] == (denial is None)
    assert len(calls(record)) == (3 if denial is None else 0)
    if denial is None:
        assert events.index("external-authorized") < events.index("provider")


@pytest.mark.it1
async def test_codex_public_provider_wiring_uses_fake_cli_and_redacts_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio
    from collections.abc import Iterator
    from contextlib import contextmanager

    from .test_semantic_answer_provider import provider_module

    data, record = fake_codex(tmp_path, fault="exit")
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data))
    module = provider_module()
    profile = ExternalAnswerProfile.model_validate(data)
    monkeypatch.setenv("DSC_EXECUTE_EXTERNAL_MODEL", "1")
    for key, value in {
        "SOCKET": "/synthetic",
        "PORT": "5432",
        "DATABASE": "synthetic",
        "USER": "synthetic",
    }.items():
        monkeypatch.setenv(f"DSC_TEST_POSTGRES_{key}", value)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-sentinel")
    monkeypatch.setenv("HTTPS_PROXY", "ambient-sentinel")

    @contextmanager
    def isolated(*args: Any, **kwargs: Any) -> Iterator[None]:
        assert kwargs["external"] is True
        yield

    async def answer(runtime: Any, char: Any, provider: Any) -> Any:
        assert isinstance(provider, CodexSubscriptionAnswerProvider)
        return await provider.complete(profile.chat, PAYLOAD)

    monkeypatch.setattr(module, "isolated_case", isolated)
    monkeypatch.setattr(module, "complete_answer", answer)
    # call_api is sync and normally run in promptfoo's own Python process.
    result = await asyncio.to_thread(
        module.call_api,
        "synonym-02",
        {"config": {"mode": "external_model", "profile_path": str(path)}},
        {"vars": {"case_id": "synonym-02"}},
    )
    assert result == {"error": "answer_evaluation_failed"}
    assert len(calls(record)) == 3
    assert capsys.readouterr() == ("", "")
    assert "sentinel" not in json.dumps(result)


@pytest.mark.it1
async def test_codex_dry_run_counts_all_cases_without_cli_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    from digital_souls_core import semantic_answer_bridge as bridge
    from digital_souls_core.semantic_answer_runtime import CountingAnswerProvider

    data, record = fake_codex(tmp_path)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data))
    for key, value in {
        "SOCKET": "/synthetic",
        "PORT": "5432",
        "DATABASE": "synthetic",
        "USER": "synthetic",
    }.items():
        monkeypatch.setenv(f"DSC_TEST_POSTGRES_{key}", value)

    @contextmanager
    def isolated(*args: Any, **kwargs: Any) -> Iterator[None]:
        assert kwargs["external"] is True
        yield

    async def answer(runtime: Any, char: Any, provider: Any) -> Any:
        assert isinstance(provider, CountingAnswerProvider)
        return await provider.complete(char.config.profile, PAYLOAD)

    monkeypatch.setattr(bridge, "isolated_case", isolated)
    monkeypatch.setattr(bridge, "complete_answer", answer)
    result = await bridge.dry_run({"profile": str(path)})
    assert result["cases"] == result["calls_per_run"] == 62
    assert result["input_tokens_per_run"] > 0
    assert calls(record) == []

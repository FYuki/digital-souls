"""External evaluation configuration and transport isolation, with synthetic secrets."""

import json
from pathlib import Path

import pytest

from digital_souls_core.semantic_answer_external import load_external_profile

from .test_semantic_retrieval_evaluation import ROOT


def external_data() -> dict[str, object]:
    embedding = json.loads((ROOT / "examples/embedding.example.json").read_text())
    embedding["enabled"] = True
    return {
        "enabled": True,
        "chat": {
            "profile_id": "external-test",
            "model": "openai/synthetic-model",
            "transport": "sdk",
            "external_send_allowed": True,
        },
        "embedding": embedding,
        "api_key_env": "SYNTHETIC_ANSWER_KEY",
        "api_base_env": "SYNTHETIC_ANSWER_BASE",
    }


@pytest.mark.ut
@pytest.mark.parametrize("fault", ["relative", "disabled", "permission", "transport", "env", "url"])
def test_external_profile_fail_closed(tmp_path: Path, fault: str) -> None:
    data = external_data()
    chat = data["chat"]
    assert isinstance(chat, dict)
    if fault == "disabled":
        data["enabled"] = False
    if fault == "permission":
        chat["external_send_allowed"] = False
    if fault == "transport":
        chat.update(
            transport="llamacpp_chat",
            model="openai/gemma4-12b",
            api_base="http://127.0.0.1:18080/v1",
        )
    if fault == "env":
        data["api_key_env"] = "INVALID-env"
    if fault == "url":
        chat["model"] = "openai/https://userinfo:secret@host/path?secret"
    path = tmp_path / "external.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Invalid external answer profile"):
        load_external_profile("relative.json" if fault == "relative" else str(path))


@pytest.mark.ut
def test_external_profile_requires_values_without_disclosing_them(tmp_path: Path) -> None:
    from digital_souls_core.semantic_answer_external import credential_environment

    path = tmp_path / "external.json"
    path.write_text(json.dumps(external_data()))
    profile = load_external_profile(str(path))
    with pytest.raises(ValueError, match="External credentials unavailable"):
        credential_environment(profile, {})
    assert credential_environment(
        profile,
        {
            "SYNTHETIC_ANSWER_KEY": "sentinel-key",
            "SYNTHETIC_ANSWER_BASE": "https://user:sentinel@host.invalid/private?sentinel",
            "UNRELATED_API_KEY": "not-forwarded",
        },
    ) == {
        "OPENAI_API_KEY": "sentinel-key",
        "OPENAI_BASE_URL": "https://user:sentinel@host.invalid/private?sentinel",
    }


@pytest.mark.it1
@pytest.mark.parametrize("denial", [None, "permission", "grant", "scanner", "classifier", "revoke"])
async def test_external_dispatch_checks_production_policy_before_provider(
    monkeypatch: pytest.MonkeyPatch,
    denial: str | None,
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace
    from typing import Any, cast

    from digital_souls_core.application import Inference
    from digital_souls_core.privacy import Permission
    from digital_souls_core.semantic_answer_runtime import FixtureAnswerProvider, complete_answer
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
    char = character("synthetic")
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

    class ObservedProvider(FixtureAnswerProvider):
        async def complete(self, profile: Any, payload: dict[str, Any]) -> dict[str, Any]:
            assert events[-1] == "check" and "external-authorized" in events
            events.append("provider")
            return await super().complete(profile, payload)

    provider = ObservedProvider("Synthetic answer")
    if denial in {"permission", "grant", "scanner", "classifier"}:
        with pytest.raises(ValueError, match="Answer evaluation failed"):
            await complete_answer(runtime, char, provider)
    else:
        observation = await complete_answer(runtime, char, provider)
        assert observation["dispatched"] == (denial is None)
    assert len(provider.payloads) == int(denial is None)
    if denial is None:
        assert events.index("external-authorized") < events.index("provider")


@pytest.mark.ut
def test_external_identity_retains_local_adapters_and_has_no_secrets(tmp_path: Path) -> None:
    from digital_souls_core.local_embedding import LocalEmbedding
    from digital_souls_core.privacy_classifier import LocalClassifier
    from digital_souls_core.semantic_answer_runtime import runtime_identity
    from digital_souls_core.semantic_evaluation_runtime import synthetic_policy

    from .test_postgres_semantic_evaluation import case

    path = tmp_path / "external.json"
    path.write_text(json.dumps(external_data()))
    identity = runtime_identity("external_model", str(path))
    assert identity["classifier_reason"] == "baseline-parity-no-classifier-export"
    assert identity["provider"] == "openai"
    assert identity["model"] == "openai/synthetic-model"
    assert identity["external_send"] is True and identity["cache"] is False
    assert identity["transport"] == "sdk" and identity["quality_evidence"] is True
    profile = load_external_profile(str(path))
    embedding = LocalEmbedding(profile.embedding)
    assert identity["space"]["dimensions"] == embedding.space.dimensions
    policy, classifier_profile = synthetic_policy(case("synonym-02"), external=True)
    assert isinstance(policy.classifier, LocalClassifier)
    assert classifier_profile.transport == "llamacpp_chat"
    assert classifier_profile.api_base == "http://127.0.0.1:18080/v1"
    assert policy.permits(case("synonym-02").binding.to_domain(), "external")
    local_policy, _ = synthetic_policy(case("synonym-02"))
    assert not local_policy.permits(case("synonym-02").binding.to_domain(), "external")
    assert "SYNTHETIC_ANSWER" not in json.dumps(identity)


@pytest.mark.ut
@pytest.mark.parametrize("mode", ["fixture", "local_model"])
def test_classifier_reason_is_absent_from_existing_identity(tmp_path: Path, mode: str) -> None:
    from digital_souls_core.semantic_answer_runtime import runtime_identity

    profile_path = None
    if mode == "local_model":
        data = json.loads((ROOT / "evals/semantic/answer-profile.example.json").read_text())
        data["enabled"] = data["embedding"]["enabled"] = True
        data["chat"]["external_send_allowed"] = True
        path = tmp_path / "local.json"
        path.write_text(json.dumps(data))
        profile_path = str(path)
    identity = runtime_identity(mode, profile_path)
    assert identity["classifier"] == "synthetic"
    assert "classifier_reason" not in identity


@pytest.mark.it1
async def test_dry_run_uses_prepared_payload_token_estimator_and_never_keeps_body() -> None:
    from digital_souls_core.contracts import Message
    from digital_souls_core.prompt_builder import ByteTokenCounter
    from digital_souls_core.semantic_answer_runtime import CountingAnswerProvider

    from .support import character

    counter = CountingAnswerProvider()
    payload = {"messages": [{"role": "user", "content": "synthetic-body-sentinel"}]}
    await counter.complete(character().config.profile, payload)
    await counter.complete(character().config.profile, payload)
    assert counter.calls == 2
    assert counter.input_tokens == 2 * ByteTokenCounter()(
        (Message.model_validate(payload["messages"][0]),)
    )
    assert counter.payloads == []


@pytest.mark.it1
def test_external_provider_public_output_redacts_sdk_stdout_stderr_and_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import os
    import sys
    from typing import Any

    from .test_semantic_answer_provider import provider_module

    path = tmp_path / "external.json"
    path.write_text(json.dumps(external_data()))
    monkeypatch.setenv("DSC_EXECUTE_EXTERNAL_MODEL", "1")
    monkeypatch.setenv("SYNTHETIC_ANSWER_KEY", "key-sentinel")
    monkeypatch.setenv("SYNTHETIC_ANSWER_BASE", "https://user:secret@host.invalid/private?secret")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    module = provider_module()

    def fail(*args: Any) -> Any:
        assert os.environ["OPENAI_API_KEY"] == "key-sentinel"
        assert "/private?secret" in os.environ["OPENAI_BASE_URL"]
        print("key-sentinel synthetic-body-sentinel")
        print("https://user:secret@host.invalid/private?secret", file=sys.stderr)
        raise RuntimeError("key-sentinel synthetic-body-sentinel")

    monkeypatch.setattr(module, "_call_api", fail)
    result = module.call_api(
        "synonym-02",
        {"config": {"mode": "external_model", "profile_path": str(path)}},
        {"vars": {"case_id": "synonym-02"}},
    )
    assert result == {"error": "answer_evaluation_failed"}
    assert capsys.readouterr() == ("", "")
    assert "OPENAI_API_KEY" not in os.environ


@pytest.mark.it1
def test_provider_external_opt_in_is_required_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .test_semantic_answer_provider import provider_module

    monkeypatch.delenv("DSC_EXECUTE_EXTERNAL_MODEL", raising=False)
    assert provider_module().call_api(
        "synonym-02",
        {"config": {"mode": "external_model", "profile_path": "/absent"}},
        {"vars": {"case_id": "synonym-02"}},
    ) == {"error": "answer_evaluation_failed"}


@pytest.mark.ut
def test_fixture_network_guard_denies_ip_without_opening_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    from typing import Any

    from .test_semantic_answer_provider import provider_module

    calls: list[object] = []

    def fake_connect(sock: socket.socket, address: Any) -> None:
        calls.append(address)

    monkeypatch.setattr(socket.socket, "connect", fake_connect)
    module = provider_module()
    module.deny_fixture_network()
    from types import SimpleNamespace
    from typing import cast

    sock = cast(socket.socket, SimpleNamespace(family=socket.AF_INET))
    with pytest.raises(RuntimeError, match="network disabled"):
        socket.socket.connect(sock, ("192.0.2.1", 443))
    with pytest.raises(RuntimeError):
        socket.socket.connect(sock, ("127.0.0.1", 18082))
    assert calls == []


@pytest.mark.ut
def test_external_profile_never_relaxes_local_validation(tmp_path: Path) -> None:
    from digital_souls_core.semantic_answer_runtime import load_profile

    data = external_data()
    data.pop("api_key_env")
    data.pop("api_base_env")
    path = tmp_path / "local.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Invalid answer profile"):
        load_profile(str(path))


@pytest.mark.ut
def test_repository_profile_cannot_enable_external_sending() -> None:
    with pytest.raises(ValueError, match="Invalid external answer profile"):
        load_external_profile(str(ROOT / "evals/semantic/external-answer-profile.example.json"))

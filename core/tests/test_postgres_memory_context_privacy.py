"""Memory application contracts on the disposable PostgreSQL fixture."""

import json
import traceback
from typing import Any

import pytest

from digital_souls_core.application import CoreError
from digital_souls_core.character import AccessScope, Profile
from digital_souls_core.contracts import CompletionInput, Message
from digital_souls_core.history import Binding, ConversationControls
from digital_souls_core.memory import MemoryContext
from digital_souls_core.memory_record_store import RetrievalCandidate
from digital_souls_core.privacy import PrivacyPolicy
from digital_souls_core.privacy_classifier import LocalClassifier

from . import test_postgres_stores
from .conversation_support import turn
from .postgres_record_support import register_sources, setup, source
from .privacy_support import BINDING
from .support import FakeProvider
from .test_postgres_stores import Stores

stores = test_postgres_stores.stores
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("during", ["base_context", "query_classification"])
async def test_inference_policy_swap_at_lookup_await_stops_old_memory_send(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, during: str
) -> None:
    service, conversation, _ = setup(stores)
    ref = await source(conversation)
    register_sources(service.records, stores.history, (ref,))
    inference = conversation.inference
    classifier = service.policy.classifier
    assert isinstance(classifier, LocalClassifier) and isinstance(
        classifier._provider, FakeProvider
    )
    classifier._provider.calls.clear()
    replacement = PrivacyPolicy()
    replacement.configure({BINDING: frozenset({"local"})})
    if during == "base_context":

        async def context(*args: Any) -> str:
            inference.privacy = replacement
            return ""

        monkeypatch.setattr(inference.context, "context", context)
    else:
        original = classifier.safe

        async def classify(value: object, version: str) -> bool:
            result = await original(value, version)
            if value == "tea":
                inference.privacy = replacement
            return result

        monkeypatch.setattr(classifier, "safe", classify)
    with pytest.raises(CoreError):
        await inference.prepare(
            "synthetic",
            CompletionInput(messages=[Message(role="user", content="tea")]),
            alias=False,
            conversation_id=ref.conversation_id,
        )
    sent = [payload["messages"][1]["content"] for _, payload in classifier._provider.calls]
    assert all("I like synthetic tea." not in value for value in sent)
    assert len(sent) == (0 if during == "base_context" else 1)


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("denial", ["permission", "sensitive", "unavailable"])
async def test_optional_memory_denial_allows_local_history_without_lookup(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, private: bool, denial: str
) -> None:
    service, conversation, _ = setup(stores)
    cid = conversation.create("synthetic").conversation_id
    revision = 0
    if private:
        conversation.controls(
            "synthetic", cid, ConversationControls(expected_revision=0, private_mode=True)
        )
        revision = 1
    if denial == "permission":
        service.policy.configure({BINDING: frozenset({"history", "local"})})
    classifier = service.policy.classifier
    assert classifier is not None
    calls = 0

    async def deny(value: object, version: str) -> bool:
        nonlocal calls
        calls += 1
        if denial == "unavailable":
            raise TimeoutError("synthetic classifier unavailable")
        return False

    def no_lookup(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Denied memory must not access storage")

    monkeypatch.setattr(classifier, "safe", deny)
    assert isinstance(conversation.inference.memory_context, MemoryContext)
    for method in ("retrievable", "current"):
        monkeypatch.setattr(
            conversation.inference.memory_context.service.records, method, no_lookup
        )
    await conversation.complete(
        "synthetic",
        cid,
        turn(
            expected_revision=revision, messages=[{"role": "user", "content": "Synthetic health"}]
        ),
    )
    assert calls == (0 if denial == "permission" else 1)
    assert isinstance(conversation.inference.provider, FakeProvider)
    payload = conversation.inference.provider.calls[-1][1]
    assert "retrieved_memory_data" not in json.dumps(payload)
    assert conversation.read("synthetic", cid).messages[0].content == "Synthetic health"


@pytest.mark.parametrize("change", ["policy", "scope", "classifier_aba"])
async def test_denied_query_with_mutation_is_not_optional(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    service, conversation, _ = setup(stores)
    classifier = service.policy.classifier
    assert isinstance(classifier, LocalClassifier)

    async def deny(value: object, version: str) -> bool:
        if change == "policy":
            service.policy.configure({})
        elif change == "scope":
            conversation.inference.scope = AccessScope(client="other")
        else:
            service.policy.classifier = None
            service.policy.classifier = classifier
        return False

    monkeypatch.setattr(classifier, "safe", deny)
    cid = conversation.create("synthetic").conversation_id
    with pytest.raises(CoreError) as caught:
        await conversation.inference.prepare(
            "synthetic",
            CompletionInput(messages=[Message(role="user", content="tea")]),
            alias=False,
            conversation_id=cid,
        )
    assert caught.value.code != "memory_query_unavailable"
    assert isinstance(conversation.inference.provider, FakeProvider)
    assert conversation.inference.provider.calls == []


@pytest.mark.parametrize("change", ["policy", "scope", "classifier", "owner"])
async def test_failed_memory_context_rechecks_same_prepared_authorization(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    service, conversation, _ = setup(stores)

    def broken(binding: Binding) -> tuple[RetrievalCandidate, ...]:
        raise CoreError(503, "storage_unavailable", "Synthetic failure")

    assert isinstance(conversation.inference.memory_context, MemoryContext)
    monkeypatch.setattr(
        conversation.inference.memory_context.service.records, "retrievable", broken
    )
    cid = conversation.create("synthetic").conversation_id
    inference = conversation.inference
    prepared = await inference.prepare(
        "synthetic",
        CompletionInput(messages=[Message(role="user", content="tea")]),
        alias=False,
        conversation_id=cid,
    )
    inference.check(prepared)
    assert len(prepared.payload["messages"]) == 2
    if change == "scope":
        inference.scope = AccessScope(client="other")
    elif change == "policy":
        service.policy.configure({})
    elif change == "classifier":
        service.policy.classifier = service.policy.classifier
    else:
        assert isinstance(inference.memory_context, MemoryContext)
        inference.memory_context.service.policy = PrivacyPolicy()
    with pytest.raises(CoreError):
        inference.check(prepared)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("change", ["payload_denied", "policy"])
async def test_failed_search_keeps_final_payload_privacy_check(
    stores: Stores,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    change: str,
    stream: bool,
) -> None:
    service, conversation, _ = setup(stores, local=False)
    query = "SYNTHETIC_QUERY_MARKER tea"

    def broken(binding: Binding) -> tuple[RetrievalCandidate, ...]:
        raise CoreError(503, "storage_unavailable", "SYNTHETIC_ERROR_MARKER")

    assert isinstance(conversation.inference.memory_context, MemoryContext)
    monkeypatch.setattr(
        conversation.inference.memory_context.service.records, "retrievable", broken
    )
    classifier = service.policy.classifier
    assert classifier is not None
    original = classifier.safe
    final_payloads: list[object] = []

    async def classify(value: object, version: str) -> bool:
        allowed = await original(value, version)
        if isinstance(value, dict) and value.get("messages", [{}])[0].get("role") == "system":
            final_payloads.append(value)
            if change == "payload_denied":
                return False
            service.policy.configure({BINDING: frozenset({"history", "local", "memory"})})
        return allowed

    monkeypatch.setattr(classifier, "safe", classify)
    provider = conversation.inference.provider
    assert isinstance(provider, FakeProvider)
    cid = conversation.create("synthetic").conversation_id
    body = turn(messages=[{"role": "user", "content": query}], stream=stream)
    with pytest.raises(CoreError) as caught:
        await conversation.complete("synthetic", cid, body)
    assert len(final_payloads) == 1
    assert provider.calls == []
    assert conversation.read("synthetic", cid).revision == 0
    assert conversation.store.receipt(BINDING, cid, body.request_id) is None
    rendered = "".join(traceback.format_exception(caught.value))
    for marker in ("synthetic_query_marker", "synthetic_error_marker"):
        assert marker not in str(caught.value).casefold()
        assert marker not in rendered.casefold()
        assert marker not in caplog.text.casefold()


async def test_failed_search_keeps_history_consent_check_after_inference(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, conversation, _ = setup(stores)

    def broken(binding: Binding) -> tuple[RetrievalCandidate, ...]:
        raise CoreError(503, "storage_unavailable", "Synthetic failure")

    assert isinstance(conversation.inference.memory_context, MemoryContext)
    monkeypatch.setattr(
        conversation.inference.memory_context.service.records, "retrievable", broken
    )
    provider = conversation.inference.provider
    assert isinstance(provider, FakeProvider)
    original = provider.complete

    async def revoke(profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        result = await original(profile, payload)
        service.policy.configure({BINDING: frozenset({"local", "memory"})})
        return result

    monkeypatch.setattr(provider, "complete", revoke)
    cid = conversation.create("synthetic").conversation_id
    body = turn(messages=[{"role": "user", "content": "tea"}])
    with pytest.raises(CoreError):
        await conversation.complete("synthetic", cid, body)
    assert len(provider.calls) == 1
    assert conversation.store.read(BINDING, cid).revision == 0
    assert conversation.store.receipt(BINDING, cid, body.request_id) is None


@pytest.mark.parametrize("change", ["scope", "policy", "classifier"])
async def test_empty_memory_context_retains_dispatch_guard(stores: Stores, change: str) -> None:
    service, conversation, _ = setup(stores)
    service.policy.configure({BINDING: frozenset({"history", "local"})})
    cid = conversation.create("synthetic").conversation_id
    prepared = await conversation.inference.prepare(
        "synthetic",
        CompletionInput(messages=[Message(role="user", content="tea")]),
        alias=False,
        conversation_id=cid,
    )
    if change == "scope":
        conversation.inference.scope = AccessScope(client="other")
    elif change == "policy":
        service.policy.configure({})
    else:
        service.policy.classifier = service.policy.classifier
    with pytest.raises(CoreError):
        conversation.inference.check(prepared)

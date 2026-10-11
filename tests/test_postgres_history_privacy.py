"""Synthetic PostgreSQL migration of the corresponding process-local contracts."""

import asyncio
import json

import pytest

from digital_souls_core.application import CoreError, Inference
from digital_souls_core.character import AccessScope
from digital_souls_core.contracts import CompletionInput, Message
from digital_souls_core.conversations import Conversations
from digital_souls_core.history import Binding, SourceReference
from digital_souls_core.privacy import PrivacyPolicy
from digital_souls_core.privacy_classifier import LocalClassifier
from digital_souls_core.prompt_builder import CharacterCard

from . import test_postgres_stores
from .conversation_support import turn
from .postgres_history_support import assert_text_absent, history
from .privacy_support import BINDING as BINDING
from .privacy_support import assessment, local_profile
from .support import CALL, TOOL, FakeProvider, character, chunk, completion
from .test_postgres_stores import Stores

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


def policy_setup(
    stores: Stores, *, local: bool = True
) -> tuple[Conversations, FakeProvider, FakeProvider, PrivacyPolicy]:
    provider = FakeProvider()
    classifier = FakeProvider()
    classifier.response["choices"][0]["message"]["content"] = assessment()
    policy = PrivacyPolicy(
        LocalClassifier(classifier, local_profile(), model_digest="synthetic-digest")
    )
    policy.configure({BINDING: frozenset({"history", "local", "external", "memory"})})
    char = character("synthetic")
    if local:
        char = type(char)(
            char.config.model_copy(update={"profile": local_profile()}),
            char.card,
            card_sha256=char.card_sha256,
        )
    inference = Inference((char,), provider, privacy=policy)
    return (
        Conversations(inference, history(stores), policy),
        provider,
        classifier,
        policy,
    )


@pytest.mark.parametrize("where", ["user", "assistant", "tool_args", "tool_result", "schema"])
async def test_secret_never_reaches_classifier_or_history(
    stores: Stores, where: str, caplog: pytest.LogCaptureFixture
) -> None:
    service, provider, classifier, _ = policy_setup(stores, local=False)
    cid = service.create("synthetic").conversation_id
    marker = "password: SYNTHETIC_ONLY"
    body = turn()
    if where == "user":
        body = turn(messages=[{"role": "user", "content": marker}])
    elif where == "assistant":
        provider.response["choices"][0]["message"]["content"] = marker
    elif where == "tool_args":
        provider.response = completion(tool=True)
        provider.response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            json.dumps({"password": "SYNTHETIC_ONLY"})
        )
        body = turn(tools=[TOOL])
    elif where == "tool_result":
        provider.response = completion(tool=True)
        await service.complete("synthetic", cid, turn(tools=[TOOL]))
        classifier.calls.clear()
        provider.calls.clear()
        body = turn(
            request_id="r2",
            expected_revision=1,
            tools=[TOOL],
            messages=[{"role": "tool", "tool_call_id": CALL["id"], "content": marker}],
        )
    else:
        tool = json.loads(json.dumps(TOOL))
        tool["function"]["description"] = marker
        body = turn(tools=[tool])
    before = service.read("synthetic", cid)
    with pytest.raises(CoreError) as error:
        await service.complete("synthetic", cid, body)
    assert "SYNTHETIC_ONLY" not in str(error.value) + caplog.text
    assert service.read("synthetic", cid) == before
    assert service.store.receipt(BINDING, cid, body.request_id) is None
    for _, payload in classifier.calls:
        assert "SYNTHETIC_ONLY" not in json.dumps(payload)
    assert_text_absent(stores, "SYNTHETIC_ONLY")


@pytest.mark.parametrize(
    "instruction", ["覚えないで", "do not remember", "保存しないで", "履歴に残さないで"]
)
async def test_natural_language_does_not_change_operation_scope(
    stores: Stores, instruction: str
) -> None:
    service, _, classifier, policy = policy_setup(stores)
    cid = service.create("synthetic").conversation_id
    body = turn(messages=[{"role": "user", "content": instruction}], memory_excluded_indices=[0])
    assert (await service.complete("synthetic", cid, body)).revision == 1
    # The caller selects the exact utterance through API metadata. Text alone is not a command.
    assert classifier.calls == []
    assert await policy.authorize(BINDING, "memory", instruction)


@pytest.mark.parametrize(
    "instruction", ["覚えないで", "do not remember", "保存しないで", "履歴に残さないで"]
)
async def test_natural_language_without_exclusion_holds_source_for_confirmation(
    stores: Stores, instruction: str
) -> None:
    service, _, _, _ = policy_setup(stores)
    cid = service.create("synthetic").conversation_id
    body = turn(messages=[{"role": "user", "content": instruction}])
    assert body.memory_excluded_indices == []
    assert (await service.complete("synthetic", cid, body)).revision == 1
    snapshot = service.read("synthetic", cid)
    assert snapshot.messages[0].content == instruction
    source = SourceReference(cid, 1, 0)
    assert snapshot.memory_sources[0].reference == source
    assert not snapshot.memory_sources[0].eligible
    assert not service.store.source_eligible(BINDING, source)
    assert not snapshot.private_mode


async def test_local_external_memory_permissions_and_sensitive_history(stores: Stores) -> None:
    service, _, classifier, policy = policy_setup(stores)
    policy.configure({BINDING: frozenset({"history", "local"})})
    cid = service.create("synthetic").conversation_id
    text = "合成人物として、治療について不安を感じています"
    await service.complete("synthetic", cid, turn(messages=[{"role": "user", "content": text}]))
    assert classifier.calls == []
    assert not await policy.authorize(BINDING, "external", text)
    assert not await policy.authorize(BINDING, "memory", text)
    policy.configure({BINDING: frozenset({"external", "memory"})})
    assert not await policy.authorize(
        BINDING, "external", text
    )  # Classifier also needs local consent.
    assert classifier.calls == []
    policy.configure({BINDING: frozenset({"local", "external", "memory"})})
    classifier.response["choices"][0]["message"]["content"] = assessment(
        classification="SENSITIVE", category="HEALTH", subject="SELF"
    )
    assert not await policy.authorize(BINDING, "external", text)
    assert not await policy.authorize(BINDING, "memory", text)
    assert not await policy.authorize(
        Binding(AccessScope(client="other"), "synthetic"), "local", "safe"
    )


async def test_revocation_during_classifier_wait(stores: Stores) -> None:
    service, _, _, policy = policy_setup(stores, local=False)
    started, release = asyncio.Event(), asyncio.Event()

    class Waiting:
        async def safe(self, value: object, policy_version: str) -> bool:
            started.set()
            await release.wait()
            return True

    policy.classifier = Waiting()
    task = asyncio.create_task(
        service.inference.prepare(
            "synthetic",
            CompletionInput(messages=[Message(role="user", content="safe")]),
            alias=False,
        )
    )
    await started.wait()
    policy.configure({})
    release.set()
    with pytest.raises(CoreError):
        await task


async def test_retry_and_output_recheck_current_grants(stores: Stores) -> None:
    service, provider, _, policy = policy_setup(stores)
    cid = service.create("synthetic").conversation_id
    first = await service.complete("synthetic", cid, turn())
    assert await service.complete("synthetic", cid, turn()) == first
    assert len(provider.calls) == 1
    policy.configure({BINDING: frozenset({"local"})})
    with pytest.raises(CoreError):
        await service.complete("synthetic", cid, turn())
    service.delete("synthetic", cid)


async def test_stream_secret_split_never_saved(stores: Stores) -> None:
    service, provider, _, _ = policy_setup(stores)
    cid = service.create("synthetic").conversation_id
    provider.chunks = [chunk({"content": t}) for t in ("pass", "word", ": SYNTHETIC_ONLY")] + [
        chunk({}, "stop")
    ]
    with pytest.raises(CoreError):
        await service.complete("synthetic", cid, turn(stream=True))
    assert service.read("synthetic", cid).messages == () and provider.closed


@pytest.mark.parametrize("source", ["system", "lore", "context"])
async def test_actual_payload_injection_blocked(stores: Stores, source: str) -> None:
    from digital_souls_core.character import Character
    from digital_souls_core.character_book import BookEntry, CharacterBook

    service, provider, classifier, _ = policy_setup(stores, local=False)
    old = service.inference.characters["synthetic"]
    marker = "password: SYNTHETIC_ONLY"
    char = Character(
        old.config,
        CharacterCard(system_prompt=marker) if source == "system" else old.card,
        character_book=CharacterBook(
            extensions={},
            entries=(
                BookEntry(
                    keys=("Synthetic",),
                    content=marker,
                    extensions={},
                    enabled=True,
                    insertion_order=0,
                    use_regex=False,
                ),
            ),
        )
        if source == "lore"
        else None,
        card_sha256=old.card_sha256,
    )

    class Context:
        async def context(self, character: Character, scope: AccessScope, user_text: str) -> str:
            return marker if source == "context" else ""

    inference = Inference((char,), provider, Context(), privacy=service.inference.privacy)
    with pytest.raises(CoreError):
        await inference.prepare("synthetic", CompletionInput(messages=turn().messages), alias=False)
    assert provider.calls == []
    assert all("SYNTHETIC_ONLY" not in json.dumps(p) for _, p in classifier.calls)


async def test_policy_replacement_prevents_context_lookup(stores: Stores) -> None:
    from digital_souls_core.character import Character

    service, provider, _, policy = policy_setup(stores, local=False)
    started, release = asyncio.Event(), asyncio.Event()
    looked_up: list[str] = []

    class Waiting:
        async def safe(self, value: object, policy_version: str) -> bool:
            started.set()
            await release.wait()
            return True

    class Context:
        async def context(self, character: Character, scope: AccessScope, user_text: str) -> str:
            looked_up.append(user_text)
            return ""

    policy.classifier = Waiting()
    service.inference.context = Context()
    task = asyncio.create_task(
        service.inference.prepare(
            "synthetic", CompletionInput(messages=turn().messages), alias=False
        )
    )
    await started.wait()
    service.inference.privacy = PrivacyPolicy()
    release.set()
    with pytest.raises(CoreError):
        await task
    assert looked_up == [] and provider.calls == []


async def test_history_policy_cannot_be_disconnected(stores: Stores) -> None:
    service, provider, _, _ = policy_setup(stores)
    cid = service.create("synthetic").conversation_id
    service.inference.privacy = None
    with pytest.raises(CoreError):
        await service.complete("synthetic", cid, turn())
    assert provider.calls == []

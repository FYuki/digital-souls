"""Real PostgreSQL, synthetic records: eligibility, races and constant batch reads."""

import json
from dataclasses import replace

import httpx
import pytest

from digital_souls_core.application import CoreError
from digital_souls_core.character import AccessScope
from digital_souls_core.contracts import Message
from digital_souls_core.history import Binding, ConversationControls, TurnDeletionInput
from digital_souls_core.local_embedding import LocalEmbedding
from digital_souls_core.memory import MemoryContext
from digital_souls_core.memory_record_store import FactWrite, RecordBatch, RetrievalCandidate
from digital_souls_core.memory_records import (
    Episode,
    EpisodeFactLink,
    ExplicitReason,
    FiveW,
    RecordState,
)
from digital_souls_core.postgres_db import PostgresDatabase
from digital_souls_core.postgres_memory_records import PostgresMemoryRecords
from digital_souls_core.privacy_classifier import LocalClassifier

from . import test_postgres_stores
from .conversation_support import turn
from .postgres_query_support import observe_queries
from .postgres_record_support import setup as conversation_setup
from .privacy_support import BINDING
from .record_retrieval_support import setup
from .support import FakeProvider, character
from .test_local_embedding_memory import profile, response
from .test_memory_record_store_contract import citation, derived, episode, fact, reference
from .test_postgres_stores import Stores, seed

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


def records(
    stores: Stores, port: PostgresMemoryRecords | None = None
) -> tuple[PostgresMemoryRecords, tuple[RetrievalCandidate, ...]]:
    if port is None:
        port = PostgresMemoryRecords(stores.database)
    e = replace(episode(citation(stores)), normalized_text="Synthetic tea experience")
    other = replace(episode(citation(stores), "e2"), normalized_text="Synthetic tea conversation")
    f = fact(citation(stores))
    link = EpisodeFactLink(
        link_id="l1",
        version=1,
        binding=BINDING,
        created_at=e.created_at,
        state=RecordState.ACTIVE,
        episode=reference(e),
        fact=reference(f),
    )
    s = replace(derived(e, other), normalized_text="Synthetic tea tendency")
    port.register(
        BINDING,
        RecordBatch(episodes=(e, other), facts=(FactWrite(f),), links=(link,), semantics=(s,)),
        "fixture-v1",
    )
    return port, (
        RetrievalCandidate(e, (f,), (link,)),
        RetrievalCandidate(other),
        RetrievalCandidate(s, evidence=(e, other)),
    )


def test_canonical_candidates_reopen(stores: Stores) -> None:
    port, expected = records(stores)
    assert port.retrievable(BINDING) == expected
    assert port.current(BINDING, expected)
    assert PostgresMemoryRecords(PostgresDatabase(stores.config)).retrievable(BINDING) == expected


@pytest.mark.parametrize(
    "other",
    [
        Binding(AccessScope(subject="other"), "synthetic"),
        Binding(AccessScope(client="other"), "synthetic"),
        Binding(AccessScope(audience="other"), "synthetic"),  # type: ignore[arg-type]
        replace(BINDING, character_id="other"),
    ],
)
async def test_exact_binding_never_embeds_other_records(stores: Stores, other: Binding) -> None:
    port, expected = records(stores)
    service, _, encoder = setup()
    service.records = port
    service.policy.configure(
        {BINDING: frozenset({"memory", "local"}), other: frozenset({"memory", "local"})}
    )
    assert port.retrievable(other) == () and not port.current(other, expected)
    assert await service.search(other, "tea") == () and encoder.calls == []


@pytest.mark.parametrize(
    "invalid",
    [
        "private",
        "private_turn",
        "excluded",
        "deleted_turn",
        "deleted_conversation",
        "epoch",
        "range",
        "speaker",
        "blocked",
        "reason",
    ],
)
def test_retrieval_rechecks_every_citation_without_relying_on_revocation(
    stores: Stores, invalid: str
) -> None:
    from digital_souls_core.memory_confirmation import pending

    port, expected = records(stores)
    e = expected[0].record
    assert isinstance(e, Episode)
    c = e.citations[0]
    if invalid == "reason":
        # Register an Episode whose independent explicit reason is withdrawn.
        reason = citation(stores)
        extra = replace(
            e,
            episode_id="reason-e",
            five_w=FiveW(predicate="heard", why=ExplicitReason("Explicit reason", (reason,))),
        )
        port.register(BINDING, RecordBatch(episodes=(extra,)), "reason-v1")
        expected = port.retrievable(BINDING)
        c = reason
    source = c.source.reference
    with stores.database.transaction(BINDING) as db:
        if invalid in {"private", "reason"}:
            db.execute(
                "UPDATE conversations SET private_mode=true WHERE id=%s", (source.conversation_id,)
            )
        elif invalid == "epoch":
            db.execute(
                "UPDATE conversations SET memory_epoch=memory_epoch+1 WHERE id=%s",
                (source.conversation_id,),
            )
        elif invalid == "deleted_conversation":
            db.execute("DELETE FROM conversations WHERE id=%s", (source.conversation_id,))
        elif invalid == "deleted_turn":
            db.execute("DELETE FROM turns WHERE conversation=%s", (source.conversation_id,))
        elif invalid == "private_turn":
            db.execute(
                "UPDATE turns SET private_mode=true WHERE conversation=%s",
                (source.conversation_id,),
            )
        elif invalid == "excluded":
            db.execute(
                "UPDATE turns SET memory_excluded='[0]' WHERE conversation=%s",
                (source.conversation_id,),
            )
        elif invalid == "blocked":
            state = pending(
                (0,),
                (
                    Message(role="user", content="Synthetic"),
                    Message(role="assistant", content="Synthetic"),
                ),
            )

            db.execute(
                "UPDATE turns SET memory_confirmation=%s WHERE conversation=%s",
                (json.dumps(state), source.conversation_id),
            )
        else:
            messages = [
                {
                    "role": "user" if invalid == "range" else "assistant",
                    "content": "x" if invalid == "range" else "Synthetic message",
                }
            ]
            db.execute(
                "UPDATE turns SET messages=%s WHERE conversation=%s",
                (json.dumps(messages), source.conversation_id),
            )
    assert not port.current(BINDING, expected)
    remaining = port.retrievable(BINDING)
    if invalid == "reason":
        assert all(r.identifier != "reason-e" for r in remaining)
        assert len(remaining) == 3
    else:
        assert remaining == (expected[1],)


@pytest.mark.parametrize(
    "invalid",
    [
        "link_suspended",
        "fact_suspended",
        "fact_version",
        "fact_source",
        "episode_suspended",
        "semantic_suspended",
        "evidence_suspended",
        "evidence_version",
    ],
)
def test_invalid_links_facts_and_semantic_dependencies(stores: Stores, invalid: str) -> None:
    port, expected = records(stores)
    if invalid == "fact_version":
        f = expected[0].facts[0]
        port.register(
            BINDING,
            RecordBatch(
                facts=(
                    FactWrite(replace(f, version=2, normalized_text="Updated synthetic fact"), 1),
                )
            ),
            "fact-v2",
        )
    else:
        with stores.database.transaction(BINDING) as db:
            if invalid == "link_suspended":
                db.execute("UPDATE memory_episode_fact_links SET state='suspended'")
            elif invalid == "fact_suspended":
                db.execute("UPDATE memory_facts SET state='suspended'")
            elif invalid == "fact_source":
                db.execute(
                    "UPDATE conversations SET private_mode=true WHERE id=%s",
                    (expected[0].facts[0].citations[0].source.reference.conversation_id,),
                )
            elif invalid == "semantic_suspended":
                db.execute("UPDATE memory_semantics SET state='suspended'")
            elif invalid == "evidence_suspended":
                db.execute("UPDATE memory_episodes SET state='suspended' WHERE id='e2'")
            elif invalid == "evidence_version":
                # A disposable fixture injects a corrupt missing reference.
                # Production foreign keys normally prevent this state.
                db.execute(
                    "ALTER TABLE memory_semantic_episodes DROP CONSTRAINT "
                    "memory_semantic_episodes_episode_fkey"
                )
                db.execute(
                    "UPDATE memory_semantic_episodes SET episode_version=2 WHERE episode='e2'"
                )
            else:
                db.execute("UPDATE memory_episodes SET state='suspended' WHERE id='e1'")
    assert not port.current(BINDING, expected)
    result = port.retrievable(BINDING)
    if invalid.startswith("fact") or invalid == "link_suspended":
        assert result[0].facts == () and result[0].links == ()
        assert [r.identifier for r in result] == ["e1", "e2", "s1"]
    elif invalid == "semantic_suspended":
        assert result == expected[:2]
    elif invalid == "episode_suspended":
        assert result == (expected[1],)
    elif invalid == "evidence_suspended":
        assert result == expected[:1]
    else:
        assert result == expected[:2]


@pytest.mark.parametrize("action", ["private", "delete", "turn_delete"])
@pytest.mark.parametrize("stage", ["embedding", "dispatch"])
@pytest.mark.parametrize("stream", [False, True])
async def test_revocation_races_stop_memory_use(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, action: str, stage: str, stream: bool
) -> None:
    port, expected = records(stores)
    service, _, encoder = setup()
    service.records = port
    writer, conversation, _ = conversation_setup(stores, local=False)
    service.policy = writer.policy
    conversation.inference.memory_context = MemoryContext(service)
    ref = expected[0].record.citations[0].source.reference

    def revoke() -> None:
        if action == "delete":
            stores.history.delete(BINDING, ref.conversation_id)
        elif action == "private":
            stores.history.controls(
                BINDING,
                ref.conversation_id,
                ConversationControls(expected_revision=1, private_mode=True),
            )
        else:
            stores.history.delete_turns(
                BINDING,
                ref.conversation_id,
                TurnDeletionInput(expected_revision=1, turn_revision=1, scope="selected"),
            )

    if stage == "embedding":
        original = encoder.embed

        async def embed(texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            result = await original(texts)
            revoke()
            return result

        monkeypatch.setattr(encoder, "embed", embed)
    else:
        classifier = service.policy.classifier
        assert isinstance(classifier, LocalClassifier)
        original_safe = classifier.safe

        async def safe(value: object, version: str) -> bool:
            result = await original_safe(value, version)
            if isinstance(value, dict) and "retrieved_memory_data" in json.dumps(value):
                revoke()
            return result

        monkeypatch.setattr(classifier, "safe", safe)
    target = conversation.create("synthetic").conversation_id
    provider = conversation.inference.provider
    assert isinstance(provider, FakeProvider)
    body = turn(messages=[{"role": "user", "content": "tea"}], stream=stream)
    if stage == "embedding":
        receipt = await conversation.complete("synthetic", target, body)
        assert receipt.revision == 1 and len(provider.calls) == 1
        assert "retrieved_memory_data" not in json.dumps(provider.calls[0][1])
    else:
        with pytest.raises(CoreError):
            await conversation.complete("synthetic", target, body)
        assert provider.calls == [] and conversation.read("synthetic", target).revision == 0
    assert not port.current(BINDING, expected)


@pytest.mark.parametrize("operation", ["retrievable", "current"])
def test_canonical_batch_statement_count_does_not_grow(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    port = PostgresMemoryRecords(stores.database)
    counts = []
    c = citation(stores)
    for count in (1, 10, 20):
        added = tuple(episode(c, f"e{count}-{i}") for i in range(count))
        port.register(BINDING, RecordBatch(episodes=added), f"fixture-{count}")
        expected = port.retrievable(BINDING)
        with observe_queries(monkeypatch) as queries:
            result = (
                port.retrievable(BINDING)
                if operation == "retrievable"
                else port.current(BINDING, expected)
            )
        assert result == (expected if operation == "retrievable" else True)
        assert all("FROM memories" not in q for q in queries.statements)
        counts.append(len(queries.statements))
    assert counts[0] <= 12 and counts == [counts[0]] * 3


async def test_sdk_receives_only_canonical_normalized_text(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, expected = records(stores)
    service, _, _ = setup()
    service.records = port
    service.embedding = LocalEmbedding(profile())
    calls = []

    async def send(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        calls.append(texts)
        return response(texts)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    result = await service.search(BINDING, "beverage")
    assert len(result) == 3
    assert calls == [["beverage", *(c.record.normalized_text for c in expected)]]
    assert "legacy" not in repr(calls) and expected[0].facts[0].normalized_text not in calls[0]


@pytest.mark.parametrize(
    "change", ["endpoint", "model", "dimensions", "delete", "candidate_endpoint"]
)
async def test_sdk_configuration_changes_fail_closed(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    port, expected = records(stores)
    service, _, _ = setup()
    service.records = port
    encoder = LocalEmbedding(profile())
    service.embedding = encoder

    async def send(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        if change == "delete":
            stores.history.delete(
                BINDING, expected[0].record.citations[0].source.reference.conversation_id
            )
        elif change == "endpoint":
            encoder._profile = encoder._profile.model_copy(
                update={"api_base": "http://127.0.0.1:18182/v1"}
            )
        elif change == "model":
            encoder._profile = encoder._profile.model_copy(update={"model": "synthetic-other"})
        elif change == "dimensions":
            encoder._profile = encoder._profile.model_copy(update={"dimensions": 3})
        else:
            pytest.fail("candidate authorization must reject before HTTP")
        return response(texts)

    if change == "candidate_endpoint":
        classifier = service.policy.classifier
        assert classifier is not None
        original = classifier.safe

        async def safe(value: object, version: str) -> bool:
            result = await original(value, version)
            if isinstance(value, list):
                encoder._profile = encoder._profile.model_copy(
                    update={"api_base": "http://127.0.0.1:18182/v1"}
                )
            return result

        monkeypatch.setattr(classifier, "safe", safe)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    with pytest.raises(CoreError):
        await service.search(BINDING, "tea")


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("identity", ["conversation", "record"])
@pytest.mark.parametrize(
    "synthetic_id", ["aaaaaaaa-aaaa-4aaa-8aaa-a09012345678", "aaaaaaaa-aaaa-4aaa-a000-000000000000"]
)
async def test_storage_identifier_collision_never_enters_context(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, local: bool, identity: str, synthetic_id: str
) -> None:
    from uuid import UUID

    from digital_souls_core import postgres_history
    from digital_souls_core.contracts import CompletionInput
    from digital_souls_core.privacy_scan import scan

    assert scan(synthetic_id).secret
    if identity == "conversation":
        monkeypatch.setattr(postgres_history, "uuid4", lambda: UUID(synthetic_id))
    c = citation(stores)
    e = replace(
        episode(c, synthetic_id if identity == "record" else "e1"),
        normalized_text="Synthetic tea experience",
    )
    port = PostgresMemoryRecords(stores.database)
    port.register(BINDING, RecordBatch(episodes=(e,)), "fixture-v1")
    service, _, _ = setup()
    service.records = port
    writer, conversation, _ = conversation_setup(stores, local=local)
    service.policy = writer.policy
    inference = conversation.inference
    inference.memory_context = MemoryContext(service)
    request = CompletionInput(messages=[Message(role="user", content="tea")])
    assert "memory_ref" not in json.dumps(
        (await inference.prepare("synthetic", request, alias=False)).payload
    )
    prepared = await inference.prepare(
        "synthetic", request, alias=False, conversation_id=c.source.reference.conversation_id
    )
    data = json.loads(prepared.payload["messages"][1]["content"].split("\n", 1)[1])
    assert synthetic_id not in json.dumps(prepared.payload)
    assert (
        data[0]["sources"][0]["conversation_ref"] == "conversation-1"
        and data[0]["memory_ref"] == "memory-1"
    )
    with pytest.raises(CoreError) as caught:
        await inference.prepare(
            "synthetic",
            CompletionInput(messages=[Message(role="user", content=synthetic_id)]),
            alias=False,
            conversation_id=c.source.reference.conversation_id,
        )
    assert caught.value.code == "privacy_denied"
    stores.history.delete(BINDING, c.source.reference.conversation_id)
    with pytest.raises(CoreError):
        inference.check(prepared)


async def test_context_sources_preserve_shared_episode_fact_and_semantic_relationships(
    stores: Stores,
) -> None:
    port, expected = records(stores)
    service, _, _ = setup()
    service.records = port
    guard = await MemoryContext(service).context(
        character("synthetic"), BINDING.scope, "tea", authorized=lambda: True
    )
    public = await service.search(BINDING, "tea")
    data = json.loads(guard.text)
    sources: dict[str, str] = {}
    for actual, framed in zip(public, data, strict=True):
        assert framed["text"] == actual.record.normalized_text
        assert "user_evidence" not in framed
        for original, sent in zip(
            dict.fromkeys(c.source for c in actual.citations), framed["sources"], strict=True
        ):
            cid = original.reference.conversation_id
            assert sent["conversation_ref"] == sources.setdefault(cid, sent["conversation_ref"])
            assert cid not in guard.text
    assert len(sources) == len(set(sources.values())) == 3
    assert all(c.identifier not in guard.text for c in expected)


@pytest.mark.parametrize("operation", ["retrievable", "current"])
def test_exact_source_pairs_and_source_count_use_constant_queries(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    port = PostgresMemoryRecords(stores.database)
    counts = []
    seed(stores, "Synthetic unrelated history marker.")
    for count in (1, 8, 16):
        with stores.database.transaction(BINDING) as db:
            db.execute("DELETE FROM memory_record_registrations")
            db.execute("DELETE FROM memory_record_citations")
            db.execute("DELETE FROM memory_episodes")
        es = tuple(episode(citation(stores), f"e{i}") for i in range(count))
        port.register(BINDING, RecordBatch(episodes=es), f"sources-{count}")
        expected = port.retrievable(BINDING)
        with observe_queries(monkeypatch) as queries:
            result = (
                port.retrievable(BINDING)
                if operation == "retrievable"
                else port.current(BINDING, expected)
            )
        assert result == (expected if operation == "retrievable" else True)
        assert len(queries.source_bodies) == count
        assert "unrelated" not in repr(queries.source_bodies)
        counts.append(len(queries.statements))
    assert counts == [counts[0]] * 3 and counts[0] <= 12


async def test_ranking_reads_persisted_mention_nulls_last_and_creation(stores: Stores) -> None:
    from datetime import timedelta

    c = citation(stores)
    base = replace(episode(c), normalized_text="Synthetic tea experience")
    values = (
        replace(base, episode_id="null", created_at=base.created_at + timedelta(days=10)),
        replace(base, episode_id="old", last_user_mentioned_at=base.created_at),
        replace(base, episode_id="new", last_user_mentioned_at=base.created_at + timedelta(days=1)),
    )
    port = PostgresMemoryRecords(stores.database)
    port.register(BINDING, RecordBatch(episodes=values), "rank-fixture")
    service, _, _ = setup()
    service.records = port
    result = await service.search(BINDING, "tea")
    assert [r.identifier for r in result] == ["new", "old", "null"]
    assert tuple(r.record for r in result) == (values[2], values[1], values[0])


@pytest.mark.parametrize("stream", [False, True])
async def test_conversation_cancel_during_embedding_never_appends(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    import asyncio

    port, _ = records(stores)
    service, _, encoder = setup()
    service.records = port
    writer, conversation, _ = conversation_setup(stores)
    service.policy = writer.policy
    conversation.inference.memory_context = MemoryContext(service)
    entered, closed = asyncio.Event(), asyncio.Event()

    async def wait(texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
        return ()

    monkeypatch.setattr(encoder, "embed", wait)
    cid = conversation.create("synthetic").conversation_id
    body = turn(messages=[{"role": "user", "content": "tea"}], stream=stream)
    task = asyncio.create_task(conversation.complete("synthetic", cid, body))
    try:
        await asyncio.wait_for(entered.wait(), 3)
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    provider = conversation.inference.provider
    assert isinstance(provider, FakeProvider)
    assert closed.is_set() and provider.calls == []
    assert conversation.read("synthetic", cid).revision == 0
    assert conversation.store.receipt(BINDING, cid, body.request_id) is None


@pytest.mark.parametrize("mismatch", ["model", "dimensions"])
async def test_sdk_model_and_dimension_response_mismatch_never_returns_memory(
    stores: Stores, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    port, _ = records(stores)
    service, _, _ = setup()
    service.records = port
    service.embedding = LocalEmbedding(profile())
    calls = 0

    async def send(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = response(json.loads(request.content)["input"]).json()
        if mismatch == "model":
            body["model"] = "synthetic-other"
        else:
            body["data"][0]["embedding"] = [1.0, 0.0, 0.0]
        return httpx.Response(200, json=body)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    with pytest.raises(CoreError) as caught:
        await service.search(BINDING, "tea")
    assert caught.value.code == "memory_embedding_failed" and calls == 1

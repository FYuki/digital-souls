"""Explicit synthetic canonical record registration and conversation harness."""

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from digital_souls_core.application import CoreError, Inference
from digital_souls_core.conversations import Conversations
from digital_souls_core.history import Binding, HistoryStore, SourceReference
from digital_souls_core.memory import MemoryContext
from digital_souls_core.memory_contracts import SourceVersion
from digital_souls_core.memory_record_store import (
    MemoryRecordStore,
    RecordBatch,
    RetrievalCandidate,
)
from digital_souls_core.memory_records import (
    Citation,
    Episode,
    EpisodeContext,
    FiveW,
    RecordState,
    Speaker,
    TemporalValue,
)
from digital_souls_core.memory_retrieval import MemoryRetrieval
from digital_souls_core.postgres_db import key
from digital_souls_core.postgres_history import PostgresHistory
from digital_souls_core.postgres_memory_records import PostgresMemoryRecords
from digital_souls_core.privacy import PrivacyPolicy
from digital_souls_core.privacy_classifier import LocalClassifier

from .conversation_support import turn
from .privacy_support import BINDING, assessment, local_profile
from .record_retrieval_support import SyntheticEmbedding
from .support import FakeProvider, character

if TYPE_CHECKING:
    from .test_postgres_stores import Stores


def source_episode(history: HistoryStore, refs: tuple[SourceReference, ...]) -> Episode:
    citations = []
    texts = []
    for ref in refs:
        snapshot = history.read(BINDING, ref.conversation_id)
        source = next((s for s in snapshot.memory_sources if s.reference == ref), None)
        if source is None:
            raise CoreError(409, "memory_record_denied", "Synthetic source ineligible")
        message = next(
            m
            for m, s in zip(snapshot.messages, snapshot.memory_sources, strict=True)
            if s.reference == ref
        )
        assert isinstance(history, PostgresHistory)
        with history.database.transaction(BINDING) as db:
            epoch = db.execute(
                "SELECT memory_epoch FROM conversations WHERE binding=%s AND id=%s",
                (key(BINDING), ref.conversation_id),
            ).fetchone()
        assert epoch is not None and message.content is not None
        citations.append(
            Citation(
                BINDING,
                SourceVersion(ref, epoch[0]),
                Speaker.USER,
                0,
                len(message.content),
            )
        )
        texts.append(message.content)
    return Episode(
        episode_id=uuid5(NAMESPACE_URL, repr(refs)).hex,
        version=1,
        binding=BINDING,
        normalized_text=json.dumps(texts, ensure_ascii=False),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_user_mentioned_at=None,
        state=RecordState.ACTIVE,
        five_w=FiveW(predicate="synthetic experience"),
        experience_time=TemporalValue(),
        experienced_at=None,
        context=EpisodeContext.ACTUAL,
        citations=tuple(citations),
    )


def register_sources(
    port: MemoryRecordStore, history: HistoryStore, refs: tuple[SourceReference, ...]
) -> tuple[RetrievalCandidate, ...]:
    record = source_episode(history, refs)
    port.register(BINDING, RecordBatch(episodes=(record,)), "fixture-v1")
    return (RetrievalCandidate(record),)


def events(port: MemoryRecordStore, binding: Binding = BINDING) -> tuple[str, ...]:
    assert isinstance(port, PostgresMemoryRecords)
    with port.database.transaction(binding) as db:
        return tuple(
            row[0]
            for row in db.execute(
                "SELECT id FROM memory_events WHERE binding=%s ORDER BY seq", (key(binding),)
            )
        )


async def source(
    conversation: Conversations, text: str = "I like synthetic tea."
) -> SourceReference:
    cid = conversation.create("synthetic").conversation_id
    context = conversation.inference.memory_context
    conversation.inference.memory_context = None
    await conversation.complete(
        "synthetic", cid, turn(messages=[{"role": "user", "content": text}])
    )
    conversation.inference.memory_context = context
    return SourceReference(cid, 1, 0)


def setup(
    stores: "Stores", *, local: bool = True
) -> tuple[MemoryRetrieval, Conversations, FakeProvider]:
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
    provider = FakeProvider()
    inference = Inference((char,), provider, privacy=policy)
    conversation = Conversations(inference, stores.history, policy)
    service = MemoryRetrieval(stores.records, policy, embedding=SyntheticEmbedding())
    inference.memory_context = MemoryContext(service)
    return service, conversation, provider

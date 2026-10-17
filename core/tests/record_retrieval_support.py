"""Synthetic canonical records and an in-process retrieval port."""

from __future__ import annotations

from dataclasses import replace

from digital_souls_core.history import Binding, SourceReference
from digital_souls_core.memory_contracts import SourceVersion
from digital_souls_core.memory_ranking import EmbeddingSpace
from digital_souls_core.memory_record_store import (
    MemoryRecord,
    MemoryRecordStore,
    RecordBatch,
    RetrievalCandidate,
)
from digital_souls_core.memory_records import Citation, RecordHead, RecordKind, RecordRef, Speaker
from digital_souls_core.memory_retrieval import MemoryRetrieval
from digital_souls_core.privacy import PrivacyPolicy
from digital_souls_core.privacy_classifier import LocalClassifier

from .privacy_support import BINDING, assessment, local_profile
from .support import FakeProvider
from .test_memory_records import episode, fact, link, semantic


def candidates() -> tuple[RetrievalCandidate, ...]:
    c = Citation(
        BINDING, SourceVersion(SourceReference("stored-conversation", 1, 0), 0), Speaker.USER, 0, 5
    )
    e = replace(
        episode(), binding=BINDING, citations=(c,), normalized_text="Synthetic tea experience"
    )
    f = replace(fact(), binding=BINDING, citations=(c,), normalized_text="Synthetic fact")
    association = replace(
        link(),
        binding=BINDING,
        episode=replace(link().episode, binding=BINDING),
        fact=replace(link().fact, binding=BINDING),
    )
    s = replace(
        semantic(), binding=BINDING, citations=(c,), normalized_text="Synthetic tea tendency"
    )
    return (RetrievalCandidate(e, (f,), (association,)), RetrievalCandidate(s))


class FakeRecords(MemoryRecordStore):
    def __init__(self) -> None:
        self.values = candidates()
        self.reads = 0
        self.checks = 0
        self.active = True

    def retrievable(self, binding: Binding) -> tuple[RetrievalCandidate, ...]:
        self.reads += 1
        return self.values if binding == BINDING and self.active else ()

    def current(self, binding: Binding, values: tuple[RetrievalCandidate, ...]) -> bool:
        self.checks += 1
        return self.active and binding == BINDING and all(v in self.values for v in values)

    def register(
        self, binding: Binding, batch: RecordBatch, formation_version: str
    ) -> tuple[RecordRef, ...]:
        raise NotImplementedError

    def get(
        self, binding: Binding, kind: RecordKind, record_id: str, *, version: int | None = None
    ) -> MemoryRecord | None:
        raise NotImplementedError

    def list(self, binding: Binding, kind: RecordKind) -> tuple[MemoryRecord, ...]:
        raise NotImplementedError

    def head(self, binding: Binding, kind: RecordKind, record_id: str) -> RecordHead | None:
        raise NotImplementedError

    def affected(self, binding: Binding, event_id: str) -> tuple[RecordRef, ...]:
        raise NotImplementedError


def setup() -> tuple[MemoryRetrieval, FakeRecords, SyntheticEmbedding]:
    provider = FakeProvider()
    provider.response["choices"][0]["message"]["content"] = assessment()
    policy = PrivacyPolicy(LocalClassifier(provider, local_profile(), model_digest="synthetic"))
    policy.configure({BINDING: frozenset({"memory", "local", "history", "external"})})
    records, embedding = FakeRecords(), SyntheticEmbedding()
    return MemoryRetrieval(records, policy, embedding=embedding), records, embedding


class SyntheticEmbedding:
    def __init__(self) -> None:
        self.space = EmbeddingSpace("synthetic", "fixture-v1", 2)
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls.append(texts)
        return tuple((1.0, 0.0) if "tea" in t or "beverage" in t else (0.0, 1.0) for t in texts)

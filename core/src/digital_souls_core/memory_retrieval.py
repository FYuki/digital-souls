"""Optional, guarded retrieval exclusively from canonical memory records."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace

from .application import CoreError
from .history import Binding
from .memory_ranking import (
    EmbeddingSpace,
    MemoryEmbedding,
    RetrievalPolicy,
    rank_records,
    validate_embedding_space,
    validate_retrieval_policy,
)
from .memory_record_store import MemoryRecordStore, RetrievalCandidate
from .privacy import PrivacyPolicy
from .privacy_classifier import LocalClassifier
from .privacy_scan import scan


class MemoryQueryUnavailable(CoreError):
    """Query refused before storage access."""

    def __init__(self) -> None:
        super().__init__(403, "memory_query_unavailable", "Memory query not authorized")


class MemoryRetrieval:
    def __init__(
        self,
        records: MemoryRecordStore,
        policy: PrivacyPolicy,
        *,
        embedding: MemoryEmbedding | None = None,
        retrieval: RetrievalPolicy | None = None,
    ) -> None:
        if not isinstance(policy.classifier, LocalClassifier):
            raise ValueError("memory requires managed local classification")
        self.records = records
        self.policy = policy
        self.retrieval = validate_retrieval_policy(retrieval or RetrievalPolicy())
        self._embedding_generation = 0
        self.embedding = embedding

    def _classifier(self) -> object:
        if not isinstance(self.policy.classifier, LocalClassifier):
            raise CoreError(403, "memory_denied", "Managed local classifier required")
        provenance = self.policy.classifier.provenance
        finding = scan(provenance)
        encoded = json.dumps(provenance, sort_keys=True)
        if finding.failed or finding.secret or len(encoded) > 4096:
            raise CoreError(403, "memory_denied", "Invalid memory provenance")
        return encoded

    def _ranking_stamp(self) -> RetrievalPolicy:
        try:
            return replace(validate_retrieval_policy(self.retrieval))
        except ValueError:
            raise CoreError(
                403, "memory_denied", "Invalid memory retrieval configuration"
            ) from None

    @property
    def embedding(self) -> MemoryEmbedding | None:
        return self._embedding

    @embedding.setter
    def embedding(self, value: MemoryEmbedding | None) -> None:
        # Trusted startup injection only. Even assigning the same instance retires
        # outstanding ranking authorizations; no vector cache crosses generations.
        self._embedding = value
        self._embedding_generation += 1

    def _retrieval_stamp(self) -> tuple[int, EmbeddingSpace | None]:
        try:
            space = None if self.embedding is None else self.embedding.space
            if self.embedding is not None:
                space = validate_embedding_space(space)
                # Snapshot scalar metadata; even a trusted adapter bypassing the
                # frozen dataclass cannot mutate a previously captured stamp.
                space = replace(space)
            return self._embedding_generation, space
        except Exception:
            raise CoreError(
                502, "memory_embedding_failed", "Invalid memory embedding configuration"
            ) from None

    async def search(
        self,
        binding: Binding,
        query: str,
        limit: int | None = None,
        *,
        authorized: Callable[[], bool] = lambda: True,
    ) -> tuple[RetrievalCandidate, ...]:
        """Return at most the policy count; an explicit limit may only lower it."""
        retrieval = self.retrieval
        ranking_stamp = self._ranking_stamp()
        if limit is None:
            limit = retrieval.max_retrieved_memories
        if (
            not isinstance(query, str)
            or not 0 < len(query) <= 256
            or type(limit) is not int
            or not 1 <= limit <= 16
        ):
            raise CoreError(400, "memory_query_invalid", "Invalid memory query")
        limit = min(limit, retrieval.max_retrieved_memories)
        policy, stamp, versions = self.policy, self.policy.stamp, self._classifier()
        embedding, stamp_retrieval = self.embedding, self._retrieval_stamp()
        memory_store = self.records

        def check(memories: tuple[RetrievalCandidate, ...] = ()) -> None:
            if (
                not authorized()
                or policy is not self.policy
                or stamp != policy.stamp
                or versions != self._classifier()
                or embedding is not self.embedding
                or stamp_retrieval != self._retrieval_stamp()
                or retrieval is not self.retrieval
                or ranking_stamp != self._ranking_stamp()
                or memory_store is not self.records
                or (memories and not memory_store.current(binding, memories))
            ):
                raise CoreError(403, "memory_denied", "Memory authorization changed")

        check()
        query_allowed = await policy.authorize(binding, "memory", query)
        check()
        if not query_allowed:
            raise MemoryQueryUnavailable()
        if embedding is None:
            return ()
        candidates = memory_store.retrievable(binding)
        texts = [text for candidate in candidates for text in candidate.texts]
        if len(candidates) > 1000 or sum(len(t.encode("utf-8")) for t in texts) > 262144:
            raise CoreError(413, "memory_limit", "Memory embedding scope exceeds limit")
        check(candidates)
        if not candidates:
            return ()
        allowed = await policy.authorize(binding, "memory", texts)
        check(candidates)
        if not allowed:
            raise CoreError(403, "memory_denied", "Memory candidates denied")
        try:
            async with asyncio.timeout(15):
                vectors = await embedding.embed(
                    (query, *(c.record.normalized_text for c in candidates))
                )
        except Exception:
            raise CoreError(502, "memory_embedding_failed", "Memory embedding failed") from None
        check(candidates)
        space = stamp_retrieval[1]
        assert space is not None
        result = rank_records(candidates, vectors, space, retrieval)[:limit]
        allowed = await policy.authorize(binding, "memory", [t for c in result for t in c.texts])
        check(candidates)
        if not allowed:
            raise CoreError(403, "memory_denied", "Memory result denied")
        return result

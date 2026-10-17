"""In-process embedding port and pure, content-free ranking matching the PoC RAG policy."""

import math
from dataclasses import dataclass, field
from typing import Protocol

from .application import CoreError
from .memory_record_store import RetrievalCandidate
from .privacy_scan import scan


@dataclass(frozen=True)
class EmbeddingSpace:
    model: str = field(repr=False)
    revision: str = field(repr=False)
    dimensions: int
    configuration: str = field(default="in-process", repr=False)

    def __post_init__(self) -> None:
        validate_embedding_space(self)


def validate_embedding_space(value: object) -> EmbeddingSpace:
    """Recheck adapter metadata at runtime, including forged dataclass instances."""
    try:
        if type(value) is not EmbeddingSpace:
            raise ValueError
        for identifier in (value.model, value.revision):
            if (
                type(identifier) is not str
                or not 0 < len(identifier) <= 128
                or identifier != identifier.strip()
            ):
                raise ValueError
            finding = scan(identifier)
            if finding.secret or finding.failed:
                raise ValueError
        if type(value.dimensions) is not int or not 1 <= value.dimensions <= 4096:
            raise ValueError
        if (
            type(value.configuration) is not str
            or not 0 < len(value.configuration) <= 1024
            or value.configuration != value.configuration.strip()
        ):
            raise ValueError
        finding = scan(value.configuration)
        if finding.secret or finding.failed:
            raise ValueError
        return value
    except Exception:
        raise CoreError(502, "memory_embedding_failed", "Memory embedding failed") from None


class MemoryEmbedding(Protocol):
    """Trusted startup-injected embedding implementation with pinned configuration."""

    @property
    def space(self) -> EmbeddingSpace: ...

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


def _unit_vector(vector: object, dimensions: int) -> tuple[float, ...]:
    if type(vector) is not tuple or len(vector) != dimensions:
        raise ValueError
    if any(type(component) not in (float, int) for component in vector):
        raise ValueError
    values = tuple(float(component) for component in vector)
    if not all(math.isfinite(component) for component in values):
        raise ValueError
    scale = max(abs(component) for component in values)
    if scale == 0:
        raise ValueError
    # Scaling before squaring keeps finite extremes and subnormal vectors usable.
    scaled = tuple(component / scale for component in values)
    norm = math.sqrt(math.fsum(component * component for component in scaled))
    return tuple(component / norm for component in scaled)


@dataclass(frozen=True)
class RetrievalPolicy:
    """PoC ranking policy with the bge-m3 threshold adopted in ADR 0024."""

    max_retrieved_memories: int = 5
    candidate_pool_size: int = 20
    relevance_threshold: float = 0.52
    equivalence_margin: float = 0.002

    def __post_init__(self) -> None:
        validate_retrieval_policy(self)


def validate_retrieval_policy(value: object) -> "RetrievalPolicy":
    if (
        type(value) is not RetrievalPolicy
        or type(value.max_retrieved_memories) is not int
        or not 1 <= value.max_retrieved_memories <= 16
        or type(value.candidate_pool_size) is not int
        or not value.max_retrieved_memories <= value.candidate_pool_size <= 1000
    ):
        raise ValueError("invalid retrieval policy")
    for number in (value.relevance_threshold, value.equivalence_margin):
        if type(number) is not float or not 0.0 <= number <= 1.0:
            raise ValueError("invalid retrieval policy")
    return value


def rank_records(
    records: tuple[RetrievalCandidate, ...],
    vectors: tuple[tuple[float, ...], ...],
    space: EmbeddingSpace,
    policy: RetrievalPolicy,
) -> tuple[RetrievalCandidate, ...]:
    """Unit-vector squared L2; metadata only breaks ties within a leader's band."""
    try:
        validate_embedding_space(space)
        validate_retrieval_policy(policy)
        if (
            type(records) is not tuple
            or any(type(r) is not RetrievalCandidate for r in records)
            or type(vectors) is not tuple
            or len(vectors) != len(records) + 1
        ):
            raise ValueError
        query, *candidates = tuple(_unit_vector(v, space.dimensions) for v in vectors)
        distances = [
            math.fsum((a - b) ** 2 for a, b in zip(query, v, strict=True)) for v in candidates
        ]
        pool = sorted(range(len(records)), key=lambda i: distances[i])[: policy.candidate_pool_size]
        relevant = [(1 / (1 + math.sqrt(distances[i])), i) for i in pool]
        relevant = [(r, i) for r, i in relevant if r >= policy.relevance_threshold]

        def tie(i: int) -> tuple[bool, float, float, str]:
            record = records[i].record
            mention = record.last_user_mentioned_at
            return (
                mention is None,
                -mention.timestamp() if mention else 0,
                -record.created_at.timestamp(),
                records[i].identifier,
            )

        ranked: list[int] = []
        band: list[int] = []
        leader = 0.0
        for relevance, index in relevant:
            if band and leader - relevance > policy.equivalence_margin:
                ranked.extend(sorted(band, key=tie))
                band = []
            if not band:
                leader = relevance
            band.append(index)
        ranked.extend(sorted(band, key=tie))
        return tuple(records[i] for i in ranked[: policy.max_retrieved_memories])
    except Exception:
        raise CoreError(502, "memory_embedding_failed", "Memory embedding failed") from None

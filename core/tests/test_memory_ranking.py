import math
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from digital_souls_core.application import CoreError
from digital_souls_core.history import SourceReference
from digital_souls_core.memory_contracts import SourceVersion
from digital_souls_core.memory_ranking import (
    EmbeddingSpace,
    MemoryEmbedding,
    RetrievalPolicy,
    rank_records,
    validate_embedding_space,
)
from digital_souls_core.memory_record_store import RetrievalCandidate
from digital_souls_core.memory_records import Citation, Speaker

from .test_memory_records import episode

pytestmark = pytest.mark.ut

SPACE = EmbeddingSpace("synthetic-embedding", "revision-one", 2)


POLICY = RetrievalPolicy()
WIDE = RetrievalPolicy(max_retrieved_memories=8, candidate_pool_size=8)


def memory(
    identifier: str, text: str = '["I enjoy synthetic tea."]', *, mentioned: int = 0
) -> RetrievalCandidate:
    citation = Citation(
        episode().binding,
        SourceVersion(SourceReference("synthetic-conversation", 3, 0), 2),
        Speaker.USER,
        0,
        5,
    )
    return RetrievalCandidate(
        replace(
            episode(),
            episode_id=identifier,
            normalized_text=text,
            citations=(citation,),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            last_user_mentioned_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=mentioned)
            if mentioned
            else None,
        )
    )


def at_cosine(cosine: float) -> tuple[float, float]:
    return (cosine, math.sqrt(max(0.0, 1.0 - cosine * cosine)))


class SyntheticEmbedding:
    @property
    def space(self) -> EmbeddingSpace:
        return SPACE

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(
            (1.0, 0.0) if "infusion" in text or "tea" in text else (0.0, 1.0) for text in texts
        )


async def test_fake_synonym_embedding_preserves_original_memory_and_provenance() -> None:
    tea, unrelated = memory("tea"), memory("trees", '["I grow synthetic trees."]')
    embedder: MemoryEmbedding = SyntheticEmbedding()
    vectors = await embedder.embed(
        ("synthetic infusion", tea.record.normalized_text, unrelated.record.normalized_text)
    )
    result = rank_records((unrelated, tea), (vectors[0], vectors[2], vectors[1]), SPACE, POLICY)
    assert result == (tea,)
    assert result[0] is tea
    assert result[0].record.citations is tea.record.citations
    assert result[0].record.citations[0].source.reference.turn_revision == 3
    assert result[0].record.citations[0].source.epoch == 2
    assert "infusion" not in tea.record.normalized_text
    assert "synthetic tea" not in repr(result)


def test_defaults_match_adr_0024_and_preserve_poc_ranking() -> None:
    assert (
        POLICY.max_retrieved_memories,
        POLICY.candidate_pool_size,
        POLICY.relevance_threshold,
        POLICY.equivalence_margin,
    ) == (5, 20, 0.52, 0.002)


def test_relevance_threshold_uses_poc_l2_mapping() -> None:
    # relevance = 1 / (1 + sqrt(squared L2)); unit vectors make L2 = 2 - 2 cosine.
    above, below = memory("above"), memory("below")
    vectors = ((1.0, 0.0), at_cosine(0.58), at_cosine(0.57))
    assert rank_records((above, below), vectors, SPACE, POLICY) == (above,)


@pytest.mark.parametrize("score,accepted", [(0.52, True), (0.52 - 1e-9, False)])
def test_default_threshold_is_inclusive_at_052(score: float, accepted: bool) -> None:
    item = memory("boundary")
    cosine = 1 - (1 / score - 1) ** 2 / 2
    vector = at_cosine(cosine)
    measured = 1 / (1 + math.dist((1.0, 0.0), vector))
    assert measured == score
    assert rank_records((item,), ((1.0, 0.0), vector), SPACE, POLICY) == (
        (item,) if accepted else ()
    )


def test_returns_at_most_max_retrieved_memories_by_relevance() -> None:
    candidates = tuple(memory(str(index)) for index in range(7))
    cosines = (0.70, 0.99, 0.80, 0.95, 0.90, 0.85, 0.75)
    vectors = ((1.0, 0.0), *(at_cosine(value) for value in cosines))
    assert rank_records(candidates, vectors, SPACE, POLICY) == tuple(
        candidates[index] for index in (1, 3, 4, 5, 2)
    )


def test_equivalence_band_prefers_latest_user_mention_then_newer_candidate() -> None:
    old_mention = memory("old-mention", mentioned=1)
    new_mention = memory("new-mention", mentioned=9)
    newer_same = memory("newer-same", mentioned=9)
    newer_same = replace(
        newer_same,
        record=replace(
            newer_same.record, created_at=newer_same.record.created_at + timedelta(seconds=1)
        ),
    )
    far = memory("far", mentioned=99)
    # The first three are within 0.002 relevance of the band leader; far is not.
    vectors = (
        (1.0, 0.0),
        at_cosine(0.9000),
        at_cosine(0.8995),
        at_cosine(0.8990),
        at_cosine(0.8500),
    )
    ranked = rank_records((old_mention, newer_same, new_mention, far), vectors, SPACE, POLICY)
    assert ranked == (newer_same, new_mention, old_mention, far)


def test_candidate_pool_limits_ranking_before_threshold_and_bands() -> None:
    policy = RetrievalPolicy(max_retrieved_memories=2, candidate_pool_size=2)
    first, second, third = memory("first"), memory("second"), memory("third", mentioned=50)
    vectors = ((1.0, 0.0), at_cosine(0.9000), at_cosine(0.8995), at_cosine(0.8990))
    # third is in the same relevance band but outside the nearest-two pool.
    assert rank_records((first, second, third), vectors, SPACE, policy) == (first, second)


def test_nonpositive_and_distant_scores_are_not_returned() -> None:
    candidates = tuple(memory(str(index)) for index in range(3))
    vectors = ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (4.0, 0.0))
    assert rank_records(candidates, vectors, SPACE, WIDE) == (candidates[2],)


@pytest.mark.parametrize(
    "values",
    [
        {"max_retrieved_memories": 0},
        {"max_retrieved_memories": True},
        {"max_retrieved_memories": 17},
        {"candidate_pool_size": 4},
        {"candidate_pool_size": 1001},
        {"relevance_threshold": -0.1},
        {"relevance_threshold": 1.1},
        {"relevance_threshold": math.nan},
        {"relevance_threshold": 1},
        {"equivalence_margin": -0.001},
        {"equivalence_margin": math.inf},
    ],
)
def test_invalid_retrieval_policy_is_rejected(values: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RetrievalPolicy(**values)


@pytest.mark.parametrize("scale", [sys.float_info.max, 1e200, 1e-200, math.ulp(0.0)])
def test_normalization_handles_large_and_small_finite_vectors(scale: float) -> None:
    positive, zero, negative = memory("positive"), memory("zero"), memory("negative")
    vectors = ((scale, scale), (scale, scale), (scale, -scale), (-scale, -scale))
    assert rank_records((positive, zero, negative), vectors, SPACE, POLICY) == (positive,)


def test_integer_vectors_and_empty_candidates() -> None:
    item = memory("integer")
    assert rank_records((item,), ((1, 2), (2, 4)), SPACE, POLICY) == (item,)
    assert rank_records((), ((1, 0),), SPACE, POLICY) == ()


def test_invalid_candidate_beyond_limit_still_rejects_the_whole_result() -> None:
    candidates = (memory("first"), memory("second"))
    policy = RetrievalPolicy(max_retrieved_memories=1, candidate_pool_size=1)
    with pytest.raises(CoreError, match="^Memory embedding failed$"):
        rank_records(candidates, ((1.0, 0.0), (1.0, 0.0), (0.0, 0.0)), SPACE, policy)


@pytest.mark.parametrize(
    "vectors",
    [
        None,
        [],
        (),
        ((1.0, 0.0),),
        ((1.0, 0.0), (1.0, 0.0), (1.0, 0.0)),
        [(1.0, 0.0), (1.0, 0.0)],
        ([1.0, 0.0], (1.0, 0.0)),
        ((1.0, 0.0), [1.0, 0.0]),
        ((1.0,), (1.0, 0.0)),
        ((1.0, 0.0), (1.0, 0.0, 0.0)),
        ((0.0, 0.0), (1.0, 0.0)),
        ((1.0, 0.0), (0.0, -0.0)),
        ((True, 0.0), (1.0, 0.0)),
        ((1.0, 0.0), (False, 1.0)),
        ((1.0, 0.0), ("private synthetic content", 1.0)),
        ((1.0, 0.0), (None, 1.0)),
        ((1.0, 0.0), (complex(1.0), 1.0)),
        ((1.0, 0.0), (math.inf, 1.0)),
        ((-math.inf, 0.0), (1.0, 1.0)),
        ((1.0, 0.0), (math.nan, 1.0)),
        ((1.0, 0.0), (10**400, 1.0)),
    ],
)
def test_invalid_vectors_fail_without_content_or_exception_chain(vectors: Any) -> None:
    with pytest.raises(CoreError) as caught:
        rank_records((memory("synthetic"),), vectors, SPACE, POLICY)
    error = caught.value
    assert (error.status, error.code, error.message) == (
        502,
        "memory_embedding_failed",
        "Memory embedding failed",
    )
    assert error.__cause__ is None and error.__suppress_context__
    assert "private synthetic content" not in repr(error)


@pytest.mark.parametrize("policy", [None, 5, (5, 20, 0.52, 0.002), "policy"])
def test_invalid_policy_fails_closed(policy: Any) -> None:
    with pytest.raises(CoreError, match="^Memory embedding failed$"):
        rank_records((memory("synthetic"),), ((1.0, 0.0), (1.0, 0.0)), SPACE, policy)


def test_forged_policy_is_rechecked() -> None:
    forged = RetrievalPolicy()
    object.__setattr__(forged, "relevance_threshold", math.nan)
    with pytest.raises(CoreError, match="^Memory embedding failed$"):
        rank_records((memory("synthetic"),), ((1.0, 0.0), (1.0, 0.0)), SPACE, forged)


@pytest.mark.parametrize(
    ("model", "revision", "dimensions"),
    [
        ("", "revision", 2),
        ("  ", "revision", 2),
        (" model", "revision", 2),
        ("model", "revision ", 2),
        ("model", "", 2),
        ("m" * 129, "revision", 2),
        ("model", "r" * 129, 2),
        (None, "revision", 2),
        ("model", 4, 2),
        ("api_key=synthetic-secret", "revision", 2),
        ("model", "api_key=synthetic-secret", 2),
        ("[invalid-json", "revision", 2),
        ("model", "[invalid-json", 2),
        ("model", "revision", True),
        ("model", "revision", 2.0),
        ("model", "revision", "2"),
        ("model", "revision", 0),
        ("model", "revision", -1),
        ("model", "revision", 4097),
    ],
)
def test_space_rejects_invalid_metadata(model: Any, revision: Any, dimensions: Any) -> None:
    with pytest.raises(CoreError, match="^Memory embedding failed$"):
        EmbeddingSpace(model, revision, dimensions)


def test_space_is_frozen_bounded_and_hides_identifiers_in_repr() -> None:
    assert validate_embedding_space(SPACE) is SPACE
    assert EmbeddingSpace("m" * 128, "r" * 128, 4096).dimensions == 4096
    assert EmbeddingSpace("m", "r", 1).dimensions == 1
    assert repr(SPACE) == "EmbeddingSpace(dimensions=2)"
    with pytest.raises(FrozenInstanceError):
        SPACE.dimensions = 3  # type: ignore[misc]


@pytest.mark.parametrize("value", [None, {}, ("model", "revision", 2), "private synthetic content"])
def test_runtime_space_validation_rejects_wrong_types(value: object) -> None:
    with pytest.raises(CoreError, match="^Memory embedding failed$"):
        validate_embedding_space(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("model", "api_key=synthetic-secret"), ("revision", "[invalid-json"), ("dimensions", True)],
)
def test_runtime_space_validation_rechecks_forged_dataclass(field: str, value: object) -> None:
    forged = EmbeddingSpace("model", "revision", 2)
    object.__setattr__(forged, field, value)
    with pytest.raises(CoreError, match="^Memory embedding failed$"):
        validate_embedding_space(forged)
    with pytest.raises(CoreError, match="^Memory embedding failed$"):
        rank_records((memory("synthetic"),), ((1.0, 0.0), (1.0, 0.0)), forged, POLICY)


def test_runtime_space_validation_rejects_uninitialized_dataclass() -> None:
    forged = object.__new__(EmbeddingSpace)
    with pytest.raises(CoreError, match="^Memory embedding failed$"):
        validate_embedding_space(forged)

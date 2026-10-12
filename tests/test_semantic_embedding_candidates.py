"""Candidate tuning contracts use synthetic vectors only."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from digital_souls_core.memory_ranking import RetrievalPolicy, rank_records
from digital_souls_core.semantic_embedding_candidates import (
    PrefixEmbedding,
    distribution,
    rank_at_threshold,
    separation_auc,
    tuning_paths,
)

from .test_memory_ranking import SPACE, at_cosine, memory

pytestmark = pytest.mark.ut


class FakeEmbedding:
    def __init__(self) -> None:
        self.space = SPACE
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls.append(texts)
        return tuple((1.0, 0.0) for _ in texts)


@pytest.mark.parametrize(
    "prefixes", [("", ""), ("search_query: ", "search_document: "), ("query: ", "passage: ")]
)
async def test_prefix_roles_preserve_order_duplicates_and_no_cache(
    prefixes: tuple[str, str],
) -> None:
    fake = FakeEmbedding()
    wrapper = PrefixEmbedding(fake, *prefixes)
    texts = ("question", "question", "document")
    for _ in range(2):
        assert await wrapper.embed(texts) == ((1.0, 0.0),) * 3
    assert (
        fake.calls
        == [(prefixes[0] + "question", prefixes[1] + "question", prefixes[1] + "document")] * 2
    )
    assert texts == ("question", "question", "document")
    assert await wrapper.embed(()) == ()
    assert len(fake.calls) == 2
    config = json.loads(wrapper.space.configuration)
    assert config["query_prefix"] == prefixes[0] and config["document_prefix"] == prefixes[1]
    with pytest.raises(FrozenInstanceError):
        wrapper.query_prefix = "changed"  # type: ignore[misc]


async def test_single_query_and_delegate_generation_are_visible() -> None:
    fake = FakeEmbedding()
    wrapper = PrefixEmbedding(fake, "query: ", "passage: ")
    before = wrapper.space
    await wrapper.embed(("question",))
    assert fake.calls == [("query: question",)]
    fake.space = replace(SPACE, revision="changed")
    assert wrapper.space != before


@pytest.mark.parametrize("threshold", [0.40, 0.54, 0.55, 0.70, 0.80])
def test_sweep_reuses_production_pool_band_limit_and_threshold(threshold: float) -> None:
    records = tuple(memory(str(i), mentioned=i) for i in range(25))
    vectors = ((7.0, 0.0), *(at_cosine(0.91 - 0.0005 * i) for i in range(25)))
    expected = rank_records(
        records, vectors, SPACE, replace(RetrievalPolicy(), relevance_threshold=threshold)
    )
    assert rank_at_threshold(records, vectors, SPACE, threshold) == expected
    assert len(expected) <= 5
    assert RetrievalPolicy().relevance_threshold == 0.52


@pytest.mark.parametrize("values", [(), (float("nan"),), (float("inf"),)])
def test_distribution_rejects_empty_or_nonfinite(values: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        distribution(values)


def test_distribution_and_auc_have_known_values_and_ties() -> None:
    assert distribution((0.1, 0.3, 0.5))["p50"] == 0.3
    assert separation_auc((0.7, 0.8), (0.1, 0.2)) == 1.0
    assert separation_auc((0.1, 0.2), (0.7, 0.8)) == 0.0
    assert separation_auc((0.5,), (0.5,)) == 0.5
    with pytest.raises(ValueError):
        separation_auc((), (0.1,))


def test_candidate_cli_can_only_select_tuning_paths() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cases, gold = tuning_paths(root)
    assert cases == root / "evals/semantic/tuning/cases.json"
    assert gold == root / "evals/semantic/tuning/expectations.json"


def test_sweep_boundary_quality_and_no_match_are_scored_without_relaxation() -> None:
    from pathlib import Path

    from digital_souls_core.semantic_embedding_candidates import summarize_sweep, sweep_case
    from digital_souls_core.semantic_evaluation_cases import load_evaluation_cases
    from digital_souls_core.semantic_retrieval_evaluation import Observation

    root = Path(__file__).resolve().parents[1]
    data = load_evaluation_cases(*tuning_paths(root))
    g = next(g for g in data.expectations.cases if g.category == "synonym")
    records = (memory(g.relevant_ids[0]),)
    # Relevance 0.55, deliberately scaled vectors: production normalizes both.
    cosine = 1 - (1 / 0.55 - 1) ** 2 / 2
    vectors = ((7.0, 0.0), tuple(v * 11 for v in at_cosine(cosine)))
    observation = Observation(
        retrieved_ids=(),
        fact_ids={},
        scores={},
        verified_ids=(),
        dispatch_valid=True,
        context_matches=True,
    )
    rows = sweep_case(g, observation, records, vectors, SPACE)
    assert len(rows) == 41
    assert next(r for r in rows if r["threshold"] == 0.52)["quality_passed"]
    assert not next(r for r in rows if r["threshold"] == 0.56)["quality_passed"]
    assert not next(r for r in rows if r["threshold"] == 0.56)["top_one_passed"]
    no_match = next(g for g in data.expectations.cases if g.no_match)
    rows = sweep_case(no_match, observation, records, vectors, SPACE)
    assert not next(r for r in rows if r["threshold"] == 0.52)["no_match_passed"]
    assert next(r for r in rows if r["threshold"] == 0.56)["no_match_passed"]
    with pytest.raises(ValueError, match="Incomplete sweep"):
        summarize_sweep(data, rows)


def test_prefix_rejects_unsupported_roles() -> None:
    with pytest.raises(ValueError):
        PrefixEmbedding(FakeEmbedding(), "passage: ", "query: ")


def test_complete_sweep_aggregates_subgroups_and_violations() -> None:
    from pathlib import Path

    from digital_souls_core.semantic_embedding_candidates import THRESHOLDS, summarize_sweep
    from digital_souls_core.semantic_evaluation_cases import load_evaluation_cases

    data = load_evaluation_cases(*tuning_paths(Path(__file__).resolve().parents[1]))
    rows = [
        dict(
            threshold=t,
            id=g.id,
            quality_passed=not g.no_match,
            top_one_passed=True,
            no_match_passed=not g.no_match,
            forbidden_ids_passed=not g.no_match,
        )
        for t in THRESHOLDS
        for g in data.expectations.cases
    ]
    summaries = summarize_sweep(data, rows)
    assert len(summaries) == 41
    for s in summaries:
        categories = s["categories"]
        assert categories["unrelated_far"]["total"] == 12
        assert categories["unrelated_near"]["forbidden_ids_failed"] == 10
        assert categories["threshold_no_match"]["no_match_failed"] == 6
        assert categories["cross_en_query"]["quality_passed"] == 12

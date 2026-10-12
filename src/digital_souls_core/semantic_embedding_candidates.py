"""Evaluation-only prefix experiments and offline production-ranking sweeps.

The fixed acceptance dataset is never selected here. Vectors are fresh for each
case/run, held only for post-observation analysis, and never serialized or cached.
"""

import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from .memory_ranking import EmbeddingSpace, MemoryEmbedding, RetrievalPolicy, rank_records
from .memory_record_store import RetrievalCandidate
from .postgres_db import PostgresConfig
from .semantic_evaluation_cases import CaseExpectation, SemanticEvaluationData
from .semantic_evaluation_runtime import FixtureEmbedding, isolated_case
from .semantic_retrieval_evaluation import (
    CaseVersion,
    Commit,
    EvaluationReport,
    Observation,
    aggregate_run,
    independent_relevance,
    observe,
    score_case,
    validate_report,
)

THRESHOLDS = tuple(i / 100 for i in range(40, 81))


@dataclass(frozen=True)
class PrefixEmbedding:
    delegate: MemoryEmbedding = field(repr=False)
    query_prefix: str = ""
    document_prefix: str = ""

    def __post_init__(self) -> None:
        if (self.query_prefix, self.document_prefix) not in {
            ("", ""),
            ("search_query: ", "search_document: "),
            ("query: ", "passage: "),
        }:
            raise ValueError("Unsupported evaluation prefixes")

    @property
    def space(self) -> EmbeddingSpace:
        original = self.delegate.space
        return replace(
            original,
            configuration=json.dumps(
                {
                    "adapter": "evaluation-prefix-v1",
                    "delegate": original.configuration,
                    "query_prefix": self.query_prefix,
                    "document_prefix": self.document_prefix,
                },
                sort_keys=True,
            ),
        )

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        return await self.delegate.embed(
            (self.query_prefix + texts[0], *(self.document_prefix + t for t in texts[1:]))
        )


class TimedEmbedding:
    def __init__(self, delegate: MemoryEmbedding) -> None:
        self.delegate = delegate
        self.calls = 0
        self.texts = 0
        self.seconds = 0.0

    @property
    def space(self) -> EmbeddingSpace:
        return self.delegate.space

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        started = perf_counter()
        result = await self.delegate.embed(texts)
        self.seconds += perf_counter() - started
        self.calls += 1
        self.texts += len(texts)
        return result

    def summary(self) -> dict[str, int | float]:
        return {
            "calls": self.calls,
            "texts": self.texts,
            "seconds": self.seconds,
            "texts_per_second": self.texts / self.seconds if self.seconds else 0.0,
        }


def tuning_paths(root: Path) -> tuple[Path, Path]:
    return (
        root / "evals/semantic/tuning/cases.json",
        root / "evals/semantic/tuning/expectations.json",
    )


def rank_at_threshold(
    records: tuple[RetrievalCandidate, ...],
    vectors: tuple[tuple[float, ...], ...],
    space: EmbeddingSpace,
    threshold: float,
) -> tuple[RetrievalCandidate, ...]:
    return rank_records(
        records, vectors, space, replace(RetrievalPolicy(), relevance_threshold=threshold)
    )


def distribution(values: tuple[float, ...]) -> dict[str, int | float]:
    if not values or not all(math.isfinite(v) for v in values):
        raise ValueError("Invalid score distribution")
    ordered = sorted(values)

    def percentile(p: float) -> float:
        position = p * (len(ordered) - 1)
        low = math.floor(position)
        high = math.ceil(position)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return {
        "count": len(values),
        "min": ordered[0],
        "p05": percentile(0.05),
        "p25": percentile(0.25),
        "p50": percentile(0.5),
        "p75": percentile(0.75),
        "p95": percentile(0.95),
        "max": ordered[-1],
        "mean": math.fsum(values) / len(values),
    }


def separation_auc(positive: tuple[float, ...], negative: tuple[float, ...]) -> float:
    distribution(positive)
    distribution(negative)
    return math.fsum(float(p > n) + 0.5 * float(p == n) for p in positive for n in negative) / (
        len(positive) * len(negative)
    )


def case_groups(gold: CaseExpectation) -> tuple[str, ...]:
    groups: list[str] = [gold.category]
    if gold.category == "unrelated":
        groups.append("unrelated_far" if "-far-" in gold.id else "unrelated_near")
    if gold.category == "threshold":
        groups.append("threshold_no_match" if gold.no_match else "threshold_match")
    if gold.category == "cross_language":
        groups.append("cross_en_query" if "-en-query-" in gold.id else "cross_ja_query")
    if gold.category in {"synonym", "paraphrase", "cross_language", "unrelated", "threshold"}:
        groups.append("daily" if "-daily-" in gold.id else "technical")
    return tuple(groups)


def sweep_case(
    gold: CaseExpectation,
    observation: Observation,
    records: tuple[RetrievalCandidate, ...],
    vectors: tuple[tuple[float, ...], ...],
    space: EmbeddingSpace,
) -> list[dict[str, Any]]:
    """Ranking/quality analysis only: never substitute it for runtime gate checks."""
    scores = (
        {
            r.identifier: independent_relevance(vectors[0], v)
            for r, v in zip(records, vectors[1:], strict=True)
        }
        if vectors
        else {}
    )
    rows: list[dict[str, Any]] = []
    for threshold in THRESHOLDS:
        ranked = rank_at_threshold(records, vectors, space, threshold) if vectors else ()
        ids = tuple(r.identifier for r in ranked)
        above = sum(v >= threshold for v in scores.values())
        derived = Observation(
            retrieved_ids=ids,
            fact_ids={r.identifier: tuple(f.fact_id for f in r.facts) for r in ranked},
            scores={i: scores[i] for i in ids},
            verified_ids=ids,
            eligible_above_threshold=above,
            dispatch_valid=observation.dispatch_valid,
            context_matches=observation.context_matches,
        )
        scored = score_case(gold, derived, mode="local_model")
        rows.append(
            {
                "threshold": threshold,
                "id": gold.id,
                "quality_passed": scored.quality_passed,
                "top_one_passed": scored.gates["top_one"],
                "no_match_passed": not gold.no_match or above == 0,
                "forbidden_ids_passed": scored.gates["forbidden_ids"],
                "retrieved_ids": ids,
            }
        )
    return rows


def summarize_sweep(
    data: SemanticEvaluationData, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    expectations = {g.id: g for g in data.expectations.cases}
    summary: list[dict[str, Any]] = []
    for threshold in THRESHOLDS:
        categories: dict[str, dict[str, int | float]] = {}
        selected = [r for r in rows if r["threshold"] == threshold]
        if len(selected) != len(expectations) or {r["id"] for r in selected} != set(expectations):
            raise ValueError("Incomplete sweep")
        for row in selected:
            for group in case_groups(expectations[row["id"]]):
                c = categories.setdefault(
                    group,
                    {
                        "total": 0,
                        "quality_passed": 0,
                        "top_one_failed": 0,
                        "no_match_failed": 0,
                        "forbidden_ids_failed": 0,
                    },
                )
                c["total"] += 1
                c["quality_passed"] += int(row["quality_passed"])
                c["top_one_failed"] += int(not row["top_one_passed"])
                c["no_match_failed"] += int(not row["no_match_passed"])
                c["forbidden_ids_failed"] += int(not row["forbidden_ids_passed"])
        for c in categories.values():
            c["rate"] = c["quality_passed"] / c["total"]
        summary.append({"threshold": threshold, "categories": categories})
    return summary


async def evaluate_candidate(
    data: SemanticEvaluationData,
    config: PostgresConfig,
    embedding: MemoryEmbedding,
    *,
    runs: int,
    mode: Literal["local_model", "fixture"] = "local_model",
    commit: Commit,
    version: CaseVersion,
    progress: Callable[[int], None] = lambda run: None,
) -> dict[str, Any]:
    if (
        type(runs) is not int
        or not 1 <= runs <= 3
        or len(data.cases.cases) != 89
        or any(not c.id.startswith("tuning-") for c in data.cases.cases)
    ):
        raise ValueError("Only the 89 tuning cases are allowed")
    delegate = embedding.delegate if isinstance(embedding, PrefixEmbedding) else embedding
    if mode not in {"local_model", "fixture"} or (
        mode == "local_model" and isinstance(delegate, FixtureEmbedding)
    ):
        raise ValueError("Invalid evaluation mode")
    timed = TimedEmbedding(embedding)
    space = timed.space
    expected = {g.id: g for g in data.expectations.cases}
    completed = []
    analyses = []
    started = perf_counter()
    for run_index in range(runs):
        case_rows = []
        sweeps: list[dict[str, Any]] = []
        measurements = []
        run_started = perf_counter()
        for case in data.cases.cases:
            case_started = perf_counter()
            with isolated_case(config, case, timed) as runtime:
                records = runtime.records.retrievable(case.binding.to_domain())
                observation = await observe(runtime)
                case_rows.append(score_case(expected[case.id], observation, mode=mode))
                calls = runtime.embedding.calls
                if len(calls) > 1:
                    raise ValueError("Unexpected embedding calls")
                vectors = calls[0].vectors if calls else ()
                if calls and calls[0].texts != (
                    case.query,
                    *(r.record.normalized_text for r in records),
                ):
                    raise ValueError("Candidate trace mismatch")
                # Baseline must match the runtime policy (ADR 0024). The #130
                # reports retain their historical 0.54 baseline and source SHA.
                ranked = (
                    rank_at_threshold(
                        records, vectors, space, RetrievalPolicy().relevance_threshold
                    )
                    if calls
                    else ()
                )
                if tuple(r.identifier for r in ranked) != observation.retrieved_ids:
                    raise ValueError("Sweep differs from production retrieval")
                measurements.append(
                    {
                        "id": case.id,
                        "embedding_called": bool(calls),
                        "relevance": {
                            r.identifier: independent_relevance(vectors[0], v)
                            for r, v in zip(records if calls else (), vectors[1:], strict=True)
                        },
                        "seconds": perf_counter() - case_started,
                    }
                )
                sweeps.extend(sweep_case(expected[case.id], observation, records, vectors, space))
            if timed.space != space:
                raise ValueError("Embedding configuration changed")
        completed.append(aggregate_run(data, tuple(case_rows), mode=mode))
        analyses.append(
            {
                "run": run_index + 1,
                "seconds": perf_counter() - run_started,
                "cases": measurements,
                "sweep": summarize_sweep(data, sweeps),
            }
        )
        progress(run_index + 1)
    report = EvaluationReport(
        commit=commit,
        mode=mode,
        quality_evidence=mode == "local_model" and timed.calls > 0,
        space=asdict(space),
        retrieval=asdict(RetrievalPolicy()),
        case_version=version,
        runs=tuple(completed),
        embedding_call_count=timed.calls,
        passed=all(r.passed for r in completed),
    )
    validate_report(report, data, runs)
    elapsed = perf_counter() - started
    return {
        "schema_version": 1,
        "purpose": "tuning-only; no acceptance or adoption decision",
        "baseline": report.model_dump(mode="json"),
        "analysis": analyses,
        "timing": {
            **timed.summary(),
            "evaluation_seconds": elapsed,
            "seconds_per_case": elapsed / (runs * len(data.cases.cases)),
        },
        "sweep_scope": "offline rank_records only; runtime gates are baseline-only",
    }

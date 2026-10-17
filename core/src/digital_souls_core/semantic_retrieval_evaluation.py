"""Production-path retrieval evaluation, independent verification, strict safe reports."""

import hashlib
import json
import math
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .local_embedding import LocalEmbedding, LocalEmbeddingProfile
from .memory_ranking import EmbeddingSpace, MemoryEmbedding, RetrievalPolicy
from .postgres_db import PostgresConfig
from .semantic_evaluation_cases import CaseExpectation, SemanticEvaluationData, expected_top_one
from .semantic_evaluation_runtime import FixtureEmbedding, PreparedCase, isolated_case


class ReportError(ValueError):
    pass


class _ReportModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)


type Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Observation(_ReportModel):
    retrieved_ids: tuple[str, ...]
    fact_ids: dict[str, tuple[str, ...]]
    scores: dict[str, Score]
    verified_ids: tuple[str, ...]
    eligible_above_threshold: Annotated[int, Field(ge=0)] = 0
    dispatch_valid: bool
    context_matches: bool
    error: Literal["evaluation_failed"] | None = None

    @model_validator(mode="after")
    def complete(self) -> "Observation":
        ids = set(self.retrieved_ids)
        if (
            self.error is not None
            or len(ids) != len(self.retrieved_ids)
            or ids != set(self.scores)
            or ids != set(self.fact_ids)
            or any(len(v) != len(set(v)) for v in self.fact_ids.values())
        ):
            raise ValueError("Invalid evaluation observation")
        return self


class CaseResult(Observation):
    id: str
    category: str
    quality_passed: bool
    gates: dict[str, bool]
    passed: bool


class CategorySummary(_ReportModel):
    total: Annotated[int, Field(gt=0)]
    passed: Annotated[int, Field(ge=0)]
    rate: Score


class RunResult(_ReportModel):
    cases: tuple[CaseResult, ...]
    categories: dict[str, CategorySummary]
    gates_passed: bool
    passed: bool


def score_case(
    gold: CaseExpectation,
    observation: Observation,
    *,
    mode: Literal["fixture", "local_model"] = "fixture",
) -> CaseResult:
    if mode not in {"fixture", "local_model"}:
        raise ReportError("Invalid evaluation mode")
    # Revalidate copies/constructed instances, never permit errors or NaN to PASS.
    observation = Observation.model_validate(observation.model_dump())
    ids = set(observation.retrieved_ids)
    attached = {f for values in observation.fact_ids.values() for f in values}
    relevant_attached = {
        f for identifier in gold.relevant_ids for f in observation.fact_ids.get(identifier, ())
    }
    quality = (
        set(gold.relevant_ids) <= ids
        and (not gold.no_match or not ids)
        and set(gold.required_fact_ids) <= relevant_attached
    )
    top_one = expected_top_one(gold)
    gates = {
        "forbidden_ids": not ids.intersection(gold.forbidden_ids),
        "threshold": all(
            score >= RetrievalPolicy().relevance_threshold for score in observation.scores.values()
        ),
        "verified_records": ids <= set(observation.verified_ids)
        and not attached.intersection(gold.forbidden_fact_ids),
        "top_one": top_one is None or top_one in observation.retrieved_ids[:5],
        "dispatch": observation.dispatch_valid == gold.dispatch.valid,
        "context": observation.context_matches,
        "no_match": not gold.no_match or observation.eligible_above_threshold == 0,
    }
    if mode == "fixture":
        gates["expected_order"] = (
            gold.expected_order is None or observation.retrieved_ids == gold.expected_order
        )
    return CaseResult(
        **observation.model_dump(),
        id=gold.id,
        category=gold.category,
        quality_passed=quality,
        gates=gates,
        passed=quality and all(gates.values()),
    )


def aggregate_run(
    data: SemanticEvaluationData,
    results: tuple[CaseResult, ...],
    *,
    mode: Literal["fixture", "local_model"] = "fixture",
) -> RunResult:
    gold = {g.id: g for g in data.expectations.cases}
    if not gold or len(results) != len(gold) or set(gold) != {r.id for r in results}:
        raise ReportError("Incomplete evaluation run")
    cases = {c.id: c for c in data.cases.cases}
    for row in results:
        case = cases[row.id]
        if not set(row.retrieved_ids) <= {r.id for r in (*case.episodes, *case.semantics)}:
            raise ReportError("Unknown returned record")
        if not {f for v in row.fact_ids.values() for f in v} <= {r.id for r in case.facts}:
            raise ReportError("Unknown attached Fact")
    checked = tuple(
        score_case(
            gold[r.id],
            Observation.model_validate(r.model_dump(include=set(Observation.model_fields))),
            mode=mode,
        )
        for r in results
    )
    if checked != results:
        raise ReportError("Inconsistent evaluation result")
    summaries: dict[str, CategorySummary] = {}
    for category in dict.fromkeys(g.category for g in data.expectations.cases):
        rows = [r for r in results if r.category == category]
        count = sum(r.quality_passed for r in rows)
        summaries[category] = CategorySummary(total=len(rows), passed=count, rate=count / len(rows))
    gates = all(all(r.gates.values()) for r in results)
    return RunResult(
        cases=results,
        categories=summaries,
        gates_passed=gates,
        passed=gates and all(s.rate >= 0.9 for s in summaries.values()),
    )


def independent_relevance(query: tuple[float, ...], candidate: tuple[float, ...]) -> float:
    """Independent unit-vector L2 verification ONLY; never decides ranking."""

    def unit(v: tuple[float, ...]) -> tuple[float, ...]:
        if not v or any(type(n) not in (int, float) or not math.isfinite(n) for n in v):
            raise ValueError("Invalid verification vector")
        scale = max(map(abs, v))
        if not scale:
            raise ValueError("Invalid verification vector")
        scaled = tuple(n / scale for n in v)
        norm = math.hypot(*scaled)
        return tuple(n / norm for n in scaled)

    if len(query) != len(candidate):
        raise ValueError("Invalid verification dimensions")
    q, c = unit(query), unit(candidate)
    return 1.0 / (1.0 + math.sqrt(sum((a - b) ** 2 for a, b in zip(q, c, strict=True))))


def select_embedding(
    data: SemanticEvaluationData, profile_path: Path | None
) -> tuple[MemoryEmbedding, Literal["fixture", "local_model"]]:
    if profile_path is None:
        return FixtureEmbedding(data), "fixture"
    try:
        profile = LocalEmbeddingProfile.model_validate_json(
            profile_path.read_text(encoding="utf-8")
        )
        if not profile.enabled:
            raise ValueError
        return LocalEmbedding(profile), "local_model"
    except Exception:
        raise ValueError("Invalid evaluation profile") from None


async def observe(runtime: PreparedCase) -> Observation:
    result, context = await runtime.search_context()
    ids = tuple(c.identifier for c in result)
    # Check valid registered snapshots before the deliberate after_search revocation.
    eligible = runtime.records.retrievable(runtime.case.binding.to_domain())
    verified = tuple(
        c.identifier
        for c in result
        if c in eligible and runtime.records.current(runtime.case.binding.to_domain(), (c,))
    )
    scores: dict[str, float] = {}
    if result:
        if len(runtime.embedding.calls) != 1:
            raise ReportError("Missing embedding trace")
        call = runtime.embedding.calls[0]
        if len(call.texts) != len(call.vectors) or call.texts[0] != runtime.case.query:
            raise ReportError("Invalid embedding trace")
        for candidate in result:
            indices = [
                i
                for i, text in enumerate(call.texts[1:], 1)
                if text == candidate.record.normalized_text
            ]
            if not indices:
                raise ReportError("Missing returned vector")
            scores[candidate.identifier] = min(
                independent_relevance(call.vectors[0], call.vectors[i]) for i in indices
            )
    # Production JSON retains normalized texts and exact Fact snapshots, not opaque IDs.
    context_rows = json.loads(context.text) if context.text else []
    matching = len(context_rows) == len(result) and all(
        row["text"] == c.record.normalized_text
        and [f["text"] for f in row["facts"]] == [f.normalized_text for f in c.facts]
        for row, c in zip(context_rows, result, strict=True)
    )
    above = 0
    if runtime.embedding.calls:
        above = max(
            sum(
                independent_relevance(call.vectors[0], vector)
                >= RetrievalPolicy().relevance_threshold
                for vector in call.vectors[1:]
            )
            for call in runtime.embedding.calls
        )
    runtime.mutate("after_search")
    dispatch_valid = context.valid()
    return Observation(
        eligible_above_threshold=above,
        retrieved_ids=ids,
        fact_ids={c.identifier: tuple(f.fact_id for f in c.facts) for c in result},
        scores=scores,
        verified_ids=verified,
        dispatch_valid=dispatch_valid,
        context_matches=matching,
    )


class CaseVersion(_ReportModel):
    schema_version: int
    expectations_schema_version: int
    cases_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    expectations_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Commit(_ReportModel):
    sha: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    dirty: bool


class EvaluationReport(_ReportModel):
    schema_version: Literal[1] = 1
    commit: Commit
    mode: Literal["fixture", "local_model"]
    quality_evidence: bool
    classifier: Literal["synthetic"] = "synthetic"
    backend_identity_verified: Literal[False] = False
    space: dict[str, str | int]
    retrieval: dict[str, int | float]
    case_version: CaseVersion
    runs: tuple[RunResult, ...]
    embedding_call_count: Annotated[int, Field(ge=0)]
    passed: bool


def validate_report(
    report: EvaluationReport, data: SemanticEvaluationData, expected_runs: int
) -> None:
    try:
        report = EvaluationReport.model_validate(report.model_dump())
        if (
            type(expected_runs) is not int
            or expected_runs < 1
            or len(report.runs) != expected_runs
            or not data.cases.cases
        ):
            raise ValueError
        if (
            report.retrieval != asdict(RetrievalPolicy())
            or report.case_version.schema_version != data.cases.schema_version
            or report.case_version.expectations_schema_version != data.expectations.schema_version
        ):
            raise ValueError
        if set(report.space) != {"model", "revision", "dimensions", "configuration"}:
            raise ValueError
        EmbeddingSpace(**report.space)  # type: ignore[arg-type]
        for run in report.runs:
            if aggregate_run(data, run.cases, mode=report.mode) != run:
                raise ValueError
        if report.quality_evidence != (
            report.mode == "local_model" and report.embedding_call_count > 0
        ) or report.passed != all(run.passed for run in report.runs):
            raise ValueError
    except Exception:
        raise ReportError("Invalid evaluation report") from None


def execution_commit(root: Path) -> Commit:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    return Commit(sha=sha, dirty=dirty)


async def evaluate_retrieval(
    data: SemanticEvaluationData,
    config: PostgresConfig,
    embedding: MemoryEmbedding,
    *,
    runs: int = 3,
    mode: Literal["fixture", "local_model"] = "fixture",
    commit: Commit,
    case_version: CaseVersion,
) -> EvaluationReport:
    if (
        type(runs) is not int
        or runs < 1
        or mode not in {"fixture", "local_model"}
        or (mode == "local_model" and isinstance(embedding, FixtureEmbedding))
    ):
        raise ReportError("Invalid evaluation configuration")
    expected = {g.id: g for g in data.expectations.cases}
    space = embedding.space
    completed = []
    calls = 0
    try:
        for _ in range(runs):
            rows = []
            for case in data.cases.cases:
                with isolated_case(config, case, embedding) as runtime:
                    rows.append(score_case(expected[case.id], await observe(runtime), mode=mode))
                    calls += len(runtime.embedding.calls)
                if embedding.space != space:
                    raise ReportError("Embedding configuration changed")
            completed.append(aggregate_run(data, tuple(rows), mode=mode))
        report = EvaluationReport(
            commit=commit,
            mode=mode,
            quality_evidence=mode == "local_model" and calls > 0,
            space=asdict(space),
            retrieval=asdict(RetrievalPolicy()),
            case_version=case_version,
            runs=tuple(completed),
            embedding_call_count=calls,
            passed=all(run.passed for run in completed),
        )
        validate_report(report, data, runs)
        return report
    except Exception:
        raise ReportError("Retrieval evaluation failed") from None


def case_version(
    data: SemanticEvaluationData, cases_path: Path, expectations_path: Path
) -> CaseVersion:
    return CaseVersion(
        schema_version=data.cases.schema_version,
        expectations_schema_version=data.expectations.schema_version,
        cases_sha256=hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        expectations_sha256=hashlib.sha256(expectations_path.read_bytes()).hexdigest(),
    )

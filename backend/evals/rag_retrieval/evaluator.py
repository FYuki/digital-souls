from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Protocol, cast

from app.memory.ranking import RetrievalRankingCandidate, rank_retrieval_candidates


@dataclass(frozen=True)
class EvaluationResult:
    recall: float
    precision: float
    irrelevant_memory_rate: float
    tie_break_accuracy: float
    privacy_boundary_violations: int
    character_boundary_violations: int
    threshold_violations: int
    unverified_fallbacks: int
    evaluated_cases: int
    retrieved_items: int
    search_latency_ms: float
    sqlite_validation_latency_ms: float
    ranking_latency_ms: float
    prompt_composition_latency_ms: float


class CandidateDistanceProvider(Protocol):
    def __call__(
        self,
        case: dict[str, object],
        candidates: list[dict[str, object]],
        query_embedding: tuple[float, ...],
        candidate_pool_size: int,
        /,
    ) -> dict[str, float]: ...


class SearchBoundaryFailure(RuntimeError):
    """評価で意図的に発生させる検索境界の障害。"""


@dataclass(frozen=True)
class _ResolvedRanking:
    candidate_pool_size: int
    max_results: int
    threshold: float
    margin: float


def evaluate_manifest(
    path: Path,
    *,
    candidate_distance_provider: CandidateDistanceProvider | None = None,
) -> EvaluationResult:
    manifest = _object(json.loads(path.read_text(encoding="utf-8")), "manifest")
    ranking = _object(manifest.get("ranking"), "ranking")
    resolved_ranking = _ResolvedRanking(
        candidate_pool_size=_integer(
            ranking.get("candidate_pool_size"), "candidate_pool_size"
        ),
        max_results=_integer(
            ranking.get("max_retrieved_memories"), "max_retrieved_memories"
        ),
        threshold=_number(
            ranking.get("relevance_threshold"), "relevance_threshold"
        ),
        margin=_number(ranking.get("equivalence_margin"), "equivalence_margin"),
    )
    evaluated_at = _datetime(manifest.get("evaluated_at"), "evaluated_at")
    policy_version = _string(manifest.get("policy_version"), "policy_version")
    cases = _objects(manifest.get("cases"), "cases")
    outcomes = [
        _evaluate_case(
            case,
            evaluated_at=evaluated_at,
            policy_version=policy_version,
            ranking=resolved_ranking,
            candidate_distance_provider=candidate_distance_provider,
        )
        for case in cases
    ]
    return _summarize(outcomes)


@dataclass(frozen=True)
class _CaseOutcome:
    expected: tuple[str, ...]
    retrieved: tuple[str, ...]
    privacy_violations: int
    character_violations: int
    threshold_violations: int
    unverified_fallbacks: int
    search_latency_ms: float
    sqlite_validation_latency_ms: float
    ranking_latency_ms: float
    prompt_composition_latency_ms: float


def _evaluate_case(
    case: dict[str, object],
    *,
    evaluated_at: datetime,
    policy_version: str,
    ranking: _ResolvedRanking,
    candidate_distance_provider: CandidateDistanceProvider | None,
) -> _CaseOutcome:
    expected = tuple(_strings(case.get("expected_ids"), "expected_ids"))
    if case.get("search_expected") is not True:
        return _CaseOutcome(expected, (), 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0)
    query = _vector(case.get("query_embedding"), "query_embedding")
    candidates = _objects(case.get("candidates"), "candidates")
    search_started = perf_counter()
    provider = (
        _fixed_distances
        if candidate_distance_provider is None
        else candidate_distance_provider
    )
    try:
        distances = provider(case, candidates, query, ranking.candidate_pool_size)
    except SearchBoundaryFailure:
        if case.get("failure") != "chroma":
            raise
        return _CaseOutcome(
            expected,
            (),
            0,
            0,
            0,
            0,
            _elapsed_ms(search_started),
            0.0,
            0.0,
            0.0,
        )
    if case.get("failure") is not None:
        raise AssertionError("declared search failure was not raised by the provider")
    search_latency_ms = _elapsed_ms(search_started)
    sqlite_started = perf_counter()
    eligible = [
        candidate
        for candidate in candidates
        if _is_verified(
            candidate,
            evaluated_at=evaluated_at,
            policy_version=policy_version,
        )
    ]
    sqlite_validation_latency_ms = _elapsed_ms(sqlite_started)
    by_id = {
        memory_id: item
        for item in eligible
        if (memory_id := _string(item.get("id"), "candidate.id")) in distances
    }
    ranking_started = perf_counter()
    ranked = rank_retrieval_candidates(
        tuple(
            RetrievalRankingCandidate(
                memory_id=memory_id,
                raw_distance=distances[memory_id],
                last_user_mentioned_at=_optional_datetime(
                    item.get("last_user_mentioned_at"), "last_user_mentioned_at"
                ),
                created_at=_datetime(item.get("created_at"), "created_at"),
            )
            for memory_id, item in by_id.items()
        ),
        relevance_threshold=ranking.threshold,
        equivalence_margin=ranking.margin,
    )[: ranking.max_results]
    ranking_latency_ms = _elapsed_ms(ranking_started)
    retrieved = tuple(item.memory_id for item in ranked)
    returned = [by_id[memory_id] for memory_id in retrieved]
    prompt_started = perf_counter()
    tuple(
        f"## 関連する記憶\n[{item['created_at']}] {item['text']}"
        for item in returned
    )
    prompt_composition_latency_ms = _elapsed_ms(prompt_started)
    return _CaseOutcome(
        expected=expected,
        retrieved=retrieved,
        privacy_violations=sum(item.get("privacy_safe") is not True for item in returned),
        character_violations=sum(item.get("character_id") != "miori" for item in returned),
        threshold_violations=sum(item.relevance < ranking.threshold for item in ranked),
        unverified_fallbacks=sum(
            not _is_verified(item, evaluated_at=evaluated_at, policy_version=policy_version)
            for item in returned
        ),
        search_latency_ms=search_latency_ms,
        sqlite_validation_latency_ms=sqlite_validation_latency_ms,
        ranking_latency_ms=ranking_latency_ms,
        prompt_composition_latency_ms=prompt_composition_latency_ms,
    )


def _fixed_distances(
    case: dict[str, object],
    candidates: list[dict[str, object]],
    query: tuple[float, ...],
    candidate_pool_size: int,
) -> dict[str, float]:
    if case.get("failure") == "chroma":
        raise SearchBoundaryFailure("injected Chroma search failure")
    distances = [
        (
            _string(item.get("id"), "candidate.id"),
            _squared_l2(
                query, _vector(item.get("embedding"), "candidate.embedding")
            ),
        )
        for item in candidates
    ]
    return dict(
        sorted(distances, key=lambda item: (item[1], item[0]))[:candidate_pool_size]
    )


def _is_verified(
    candidate: dict[str, object], *, evaluated_at: datetime, policy_version: str
) -> bool:
    expires_at = _optional_datetime(candidate.get("expires_at"), "expires_at")
    return (
        candidate.get("persisted") is True
        and candidate.get("character_id") == "miori"
        and candidate.get("provider_id") == "core"
        and candidate.get("status") == "ACTIVE"
        and (expires_at is None or expires_at > evaluated_at)
        and candidate.get("policy_version") == policy_version
        and candidate.get("privacy_safe") is True
    )


def _summarize(outcomes: list[_CaseOutcome]) -> EvaluationResult:
    expected_count = sum(len(outcome.expected) for outcome in outcomes)
    retrieved_count = sum(len(outcome.retrieved) for outcome in outcomes)
    true_positive_count = sum(
        len(set(outcome.expected) & set(outcome.retrieved)) for outcome in outcomes
    )
    ordered_cases = [outcome for outcome in outcomes if len(outcome.expected) > 1]
    correct_order_count = sum(
        outcome.retrieved == outcome.expected for outcome in ordered_cases
    )
    return EvaluationResult(
        recall=true_positive_count / expected_count if expected_count else 1.0,
        precision=true_positive_count / retrieved_count if retrieved_count else 1.0,
        irrelevant_memory_rate=(retrieved_count - true_positive_count) / retrieved_count
        if retrieved_count
        else 0.0,
        tie_break_accuracy=correct_order_count / len(ordered_cases)
        if ordered_cases
        else 1.0,
        privacy_boundary_violations=sum(item.privacy_violations for item in outcomes),
        character_boundary_violations=sum(item.character_violations for item in outcomes),
        threshold_violations=sum(item.threshold_violations for item in outcomes),
        unverified_fallbacks=sum(item.unverified_fallbacks for item in outcomes),
        evaluated_cases=len(outcomes),
        retrieved_items=retrieved_count,
        search_latency_ms=sum(item.search_latency_ms for item in outcomes),
        sqlite_validation_latency_ms=sum(
            item.sqlite_validation_latency_ms for item in outcomes
        ),
        ranking_latency_ms=sum(item.ranking_latency_ms for item in outcomes),
        prompt_composition_latency_ms=sum(
            item.prompt_composition_latency_ms for item in outcomes
        ),
    )


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000


def _squared_l2(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions must match")
    return sum((left_value - right_value) ** 2 for left_value, right_value in zip(left, right, strict=True))


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _objects(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return [_object(item, label) for item in value]


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a string array")
    return cast(list[str], value)


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return value


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    return float(value)


def _vector(value: object, label: str) -> tuple[float, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty number array")
    return tuple(_number(item, label) for item in value)


def _datetime(value: object, label: str) -> datetime:
    return datetime.fromisoformat(_string(value, label))


def _optional_datetime(value: object, label: str) -> datetime | None:
    return None if value is None else _datetime(value, label)

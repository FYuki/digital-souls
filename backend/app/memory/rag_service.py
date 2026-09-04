import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.memory.chroma_store import (
    EmbeddingFingerprint,
    MemorySearchCandidate,
    MemorySearchResult,
    RetrievalMatchKind,
    query_memories,
)
from app.memory.memory_policy import MemoryPolicy, rag_service_policy
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.contracts import (
    ApprovedMemory,
    MemoryStatus,
    TemporalPrecision,
)
from app.memory.ranking import RetrievalRankingCandidate, rank_retrieval_candidates
from app.memory.temporal_query import (
    TemporalQuery,
    TemporalQueryKind,
    match_season,
    parse_temporal_query,
)
from app.privacy.contracts import PrivacyScanner, ScanFailure, ScanSuccess
from app.privacy.semantic.classifier import SemanticPrivacyClassifier
from app.privacy.semantic.contracts import (
    QUERY_GATE,
    PrivacyAssessment,
    SemanticAssessmentReasonCode,
    SemanticClassification,
)

logger = logging.getLogger(__name__)
RAG_OPERATION_ERRORS = (
    httpx.HTTPError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.Error,
)


@dataclass(frozen=True)
class _VerifiedCandidate:
    candidate: MemorySearchCandidate
    memory: ApprovedMemory


@dataclass(frozen=True)
class RetrievalOutcome:
    memories: tuple[MemorySearchResult, ...]
    no_match: bool


def embed_text(_text: str) -> list[float]:
    """テスト互換用の未設定境界。productionは必ず依存を注入する。"""

    raise RuntimeError("memory embedder is not configured")


def retrieve_prompt_memories(
    character: str,
    user_message: str,
    policy: MemoryPolicy,
    *,
    scanner: PrivacyScanner,
    classifier: SemanticPrivacyClassifier,
    approved_repository: ApprovedMemoryRepository,
    embedder: Callable[[str], list[float]] | None = None,
    chroma_path: Path,
    now: datetime,
    timezone: str,
) -> RetrievalOutcome:
    try:
        query_scan = scanner.scan(user_message)
        if _scan_blocks_retrieval(query_scan, policy):
            return RetrievalOutcome((), False)
        assessment = classifier.classify(user_message, QUERY_GATE)
        if (
            not isinstance(assessment, PrivacyAssessment)
            or not isinstance(assessment.classification, SemanticClassification)
            or not isinstance(assessment.reason_code, SemanticAssessmentReasonCode)
        ):
            logger.warning("Skipped RAG memory lookup: invalid semantic assessment")
            return RetrievalOutcome((), False)
        if assessment.classification is not SemanticClassification.NOT_SENSITIVE:
            logger.warning(
                "Skipped RAG memory lookup: semantic_reason_code=%s",
                assessment.reason_code.value,
            )
            return RetrievalOutcome((), False)
        ranking_policy = rag_service_policy(policy)
        temporal_query = parse_temporal_query(
            user_message,
            now=now,
            timezone=timezone,
        )
        period_memories = (
            []
            if temporal_query is None
            else approved_repository.search_by_occurred_range(
                character_id=character,
                start=temporal_query.start,
                end=temporal_query.end,
                compatible_policy_versions=(
                    frozenset(policy.retrieval_compatible_policy_versions)
                ),
            )
        )
        resolved_embedder = embed_text if embedder is None else embedder
        embedding = resolved_embedder(user_message)
        candidates = query_memories(
            character,
            embedding,
            n_results=ranking_policy.candidate_pool_size,
            chroma_path=chroma_path,
            fingerprint=_embedding_fingerprint(resolved_embedder, embedding),
        )
        verified = _verified_candidates(
            candidates,
            character=character,
            policy=policy,
            scanner=scanner,
            approved_repository=approved_repository,
            now=now.astimezone(UTC),
        )
        ranked = _rank_candidates(
            verified,
            relevance_threshold=ranking_policy.relevance_threshold,
            equivalence_margin=ranking_policy.equivalence_margin,
        )
        if temporal_query is None:
            return RetrievalOutcome(
                tuple(
                    _search_result(item, RetrievalMatchKind.SEMANTIC)
                    for item in ranked[: ranking_policy.max_retrieved_memories]
                ),
                False,
            )
        period_memories = _verified_period_memories(
            period_memories,
            character=character,
            policy=policy,
            scanner=scanner,
            now=now.astimezone(UTC),
        )
        period_memories = _filter_period_memories(period_memories, temporal_query)
        combined = _rank_temporal_candidates(ranked, period_memories)
        memories = tuple(
            _search_result(candidate, match_kind)
            for candidate, match_kind in combined[
                : ranking_policy.max_retrieved_memories
            ]
        )
        return RetrievalOutcome(memories, not memories)
    except RAG_OPERATION_ERRORS as exc:
        logger.warning("RAG memory lookup failed: %s", exc.__class__.__name__)
        return RetrievalOutcome((), False)


def _embedding_fingerprint(
    embedder: Callable[[str], list[float]], embedding: list[float]
) -> EmbeddingFingerprint | None:
    provider_id = getattr(embedder, "provider_id", None)
    model_id = getattr(embedder, "model_id", None)
    if not isinstance(provider_id, str) or not isinstance(model_id, str):
        return None
    return EmbeddingFingerprint(provider_id, model_id, len(embedding))


def _scan_blocks_retrieval(scan: object, policy: MemoryPolicy) -> bool:
    if isinstance(scan, ScanFailure):
        logger.warning(
            "Skipped RAG memory lookup: scan_failure_reason_code=%s "
            "recognizer_version=%s policy_version=%s",
            scan.reason_code.value,
            scan.recognizer_version,
            scan.policy_version,
        )
        return True
    if not isinstance(scan, ScanSuccess):
        raise TypeError("privacy scanner returned an unsupported result")
    blocked = any(
        finding.category in policy.privacy.absolute_deny_categories
        for finding in scan.findings
    )
    if blocked:
        logger.warning("Skipped RAG memory lookup by deterministic privacy policy")
    return blocked


def _verified_candidates(
    candidates: list[MemorySearchCandidate],
    *,
    character: str,
    policy: MemoryPolicy,
    scanner: PrivacyScanner,
    approved_repository: ApprovedMemoryRepository,
    now: datetime,
) -> tuple[_VerifiedCandidate, ...]:
    verified: list[_VerifiedCandidate] = []
    for candidate in candidates:
        try:
            memory_id = UUID(candidate.memory_id)
        except ValueError:
            logger.warning("Excluded RAG memory candidate with malformed ID")
            continue
        memory = approved_repository.get(
            character_id=character,
            memory_id=memory_id,
        )
        if not _is_retrieval_compatible(memory, character, policy, now):
            logger.warning("Excluded RAG memory candidate by SQLite policy")
            continue
        assert memory is not None
        body_scan = scanner.scan(memory.normalized_text)
        if not isinstance(body_scan, (ScanFailure, ScanSuccess)):
            logger.warning(
                "Excluded RAG memory candidate: unsupported privacy scan result"
            )
            continue
        if _scan_blocks_retrieval(body_scan, policy):
            continue
        verified.append(
            _VerifiedCandidate(
                candidate=candidate,
                memory=memory,
            )
        )
    return tuple(verified)


def _rank_candidates(
    candidates: tuple[_VerifiedCandidate, ...],
    *,
    relevance_threshold: float,
    equivalence_margin: float,
) -> tuple[_VerifiedCandidate, ...]:
    by_id = {str(candidate.memory.id): candidate for candidate in candidates}
    ranked = rank_retrieval_candidates(
        tuple(
            RetrievalRankingCandidate(
                memory_id=str(candidate.memory.id),
                raw_distance=candidate.candidate.raw_distance,
                last_user_mentioned_at=candidate.memory.last_user_mentioned_at,
                created_at=candidate.memory.created_at,
            )
            for candidate in candidates
        ),
        relevance_threshold=relevance_threshold,
        equivalence_margin=equivalence_margin,
    )
    return tuple(by_id[candidate.memory_id] for candidate in ranked)


def _filter_period_memories(
    memories: list[ApprovedMemory], query: TemporalQuery
) -> list[ApprovedMemory]:
    if query.kind is TemporalQueryKind.SEASON:
        return [
            memory
            for memory in memories
            if match_season(
                query,
                occurred_at=memory.occurred_at,
                occurred_precision=memory.occurred_precision,
                occurred_timezone=memory.occurred_timezone,
            ).matched
        ]
    allowed_precisions = (
        frozenset(
            {
                TemporalPrecision.MONTH,
                TemporalPrecision.DAY,
                TemporalPrecision.HOUR,
                TemporalPrecision.MINUTE,
                TemporalPrecision.SECOND,
            }
        )
        if query.kind is TemporalQueryKind.MONTH
        else frozenset(
            {
                TemporalPrecision.DAY,
                TemporalPrecision.HOUR,
                TemporalPrecision.MINUTE,
                TemporalPrecision.SECOND,
            }
        )
    )
    return [
        memory
        for memory in memories
        if memory.occurred_precision in allowed_precisions
    ]


def _verified_period_memories(
    memories: list[ApprovedMemory],
    *,
    character: str,
    policy: MemoryPolicy,
    scanner: PrivacyScanner,
    now: datetime,
) -> list[ApprovedMemory]:
    verified: list[ApprovedMemory] = []
    for memory in memories:
        if not _is_retrieval_compatible(memory, character, policy, now):
            continue
        body_scan = scanner.scan(memory.normalized_text)
        if not isinstance(body_scan, (ScanFailure, ScanSuccess)):
            continue
        if not _scan_blocks_retrieval(body_scan, policy):
            verified.append(memory)
    return verified


def _rank_temporal_candidates(
    semantic: tuple[_VerifiedCandidate, ...],
    period: list[ApprovedMemory],
) -> tuple[tuple[_VerifiedCandidate, RetrievalMatchKind], ...]:
    semantic_by_id = {candidate.memory.id: candidate for candidate in semantic}
    period_by_id = {memory.id: memory for memory in period}
    both = tuple(
        (candidate, RetrievalMatchKind.BOTH)
        for candidate in semantic
        if candidate.memory.id in period_by_id
    )
    semantic_only = tuple(
        (candidate, RetrievalMatchKind.SEMANTIC)
        for candidate in semantic
        if candidate.memory.id not in period_by_id
    )
    period_only_memories = sorted(
        (
            memory
            for memory in period
            if memory.id not in semantic_by_id
        ),
        key=lambda memory: (
            memory.last_user_mentioned_at or datetime.min.replace(tzinfo=UTC),
            memory.created_at,
            str(memory.id),
        ),
        reverse=True,
    )
    period_only = tuple(
        (
            _VerifiedCandidate(
                MemorySearchCandidate(str(memory.id), float("inf")),
                memory,
            ),
            RetrievalMatchKind.PERIOD,
        )
        for memory in period_only_memories
    )
    return (*both, *semantic_only, *period_only)


def _search_result(
    candidate: _VerifiedCandidate, match_kind: RetrievalMatchKind
) -> MemorySearchResult:
    memory = candidate.memory
    return MemorySearchResult(
        memory_id=str(memory.id),
        normalized_text=memory.normalized_text,
        occurred_at=_format_occurred_at(memory),
        occurred_precision=memory.occurred_precision,
        match_kind=match_kind,
        memory_type=memory.memory_type.value,
        raw_distance=candidate.candidate.raw_distance,
    )


def _format_occurred_at(memory: ApprovedMemory) -> str | None:
    if memory.occurred_at is None or memory.occurred_timezone is None:
        return None
    try:
        zone = ZoneInfo(memory.occurred_timezone)
    except ZoneInfoNotFoundError:
        return None
    return memory.occurred_at.astimezone(zone).isoformat()


def _is_retrieval_compatible(
    memory: ApprovedMemory | None,
    character: str,
    policy: MemoryPolicy,
    now: datetime,
) -> bool:
    return (
        memory is not None
        and memory.character_id == character
        and memory.provider_id == "core"
        and memory.status is MemoryStatus.ACTIVE
        and (memory.expires_at is None or memory.expires_at > now)
        and memory.policy_version in policy.retrieval_compatible_policy_versions
    )

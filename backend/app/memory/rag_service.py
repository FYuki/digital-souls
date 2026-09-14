import json
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
from app.memory.read_contracts import EpisodicMemoryView, MemoryReadRepository, ReadableMemory, SemanticMemoryView
from app.memory.episodic.temporal import render_time
from app.memory.episodic.time_search import matches_time
from app.memory.persistence.contracts import (
    MemoryStatus,
    TemporalPrecision,
)
from app.memory.ranking import RetrievalRankingCandidate, rank_retrieval_candidates
from app.memory.semantic.read_repository import WithSemanticReadRepository
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
    memory: ReadableMemory
    match_kind: RetrievalMatchKind | None = None


@dataclass(frozen=True)
class RetrievalOutcome:
    memories: tuple[MemorySearchResult, ...]
    no_match: bool
    response_cautions: tuple[str, ...] = ()


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
    approved_repository: MemoryReadRepository,
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
        cautions = _semantic_response_cautions(
            approved_repository, character=character, query=user_message,
            policy=policy, scanner=scanner,
        )
        ranking_policy = rag_service_policy(policy)
        temporal_query = parse_temporal_query(
            user_message,
            now=now,
            timezone=timezone,
        )
        period_memories = (
            []
            if temporal_query is None
            else list(approved_repository.search_by_occurred_range(
                character_id=character,
                start=temporal_query.start,
                end=temporal_query.end,
                compatible_policy_versions=(
                    frozenset(policy.retrieval_compatible_policy_versions)
                ),
            ))
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
            ranked = _include_lexical_semantics(
                ranked, character=character, query=user_message, policy=policy, scanner=scanner,
                approved_repository=approved_repository, now=now.astimezone(UTC),
            )
            ranked = _include_current_self_reports(
                ranked, character=character, policy=policy, scanner=scanner,
                approved_repository=approved_repository, now=now.astimezone(UTC),
            )
            return RetrievalOutcome(
                tuple(
                    _search_result(item, RetrievalMatchKind.SEMANTIC)
                    for item in ranked[: ranking_policy.max_retrieved_memories]
                ),
                False,
                cautions,
            )
        period_memories = _verified_period_memories(
            period_memories,
            character=character,
            policy=policy,
            scanner=scanner,
            approved_repository=approved_repository,
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
        return RetrievalOutcome(memories, not memories, cautions)
    except RAG_OPERATION_ERRORS as exc:
        logger.warning("RAG memory lookup failed: %s", exc.__class__.__name__)
        return RetrievalOutcome((), False)


def _include_lexical_semantics(
    ranked: tuple[_VerifiedCandidate, ...], *, character: str, query: str,
    policy: MemoryPolicy, scanner: PrivacyScanner, approved_repository: MemoryReadRepository, now: datetime,
) -> tuple[_VerifiedCandidate, ...]:
    if not isinstance(approved_repository, WithSemanticReadRepository):
        return ranked
    snapshots: list[ReadableMemory] = list(approved_repository.semantic.search_by_text(
        character_id=character, query=query,
    ))
    verified = _verified_period_memories(snapshots, character=character, policy=policy,
        scanner=scanner, approved_repository=approved_repository, now=now)
    by_id = {item.memory.id: item for item in ranked}
    supplemental = tuple(_VerifiedCandidate(
        by_id[memory.id].candidate if memory.id in by_id else MemorySearchCandidate(str(memory.id), float("inf")),
        memory, RetrievalMatchKind.LEXICAL,
    ) for memory in verified)
    selected_ids = {item.memory.id for item in supplemental}
    return (*supplemental, *(item for item in ranked if item.memory.id not in selected_ids))

def _semantic_response_cautions(
    repository: MemoryReadRepository, *, character: str, query: str,
    policy: MemoryPolicy, scanner: PrivacyScanner,
) -> tuple[str, ...]:
    if not isinstance(repository, WithSemanticReadRepository):
        return ()
    cautions: list[str] = []
    for record in repository.semantic.list_conflicted(character_id=character):
        value = record.proposition
        if (value is None or record.character_id != character
                or record.stamp.policy_version not in policy.retrieval_compatible_policy_versions
                or value.predicate.casefold() not in query.casefold()):
            continue
        caution = json.dumps({"subject": value.subject, "attribute": value.predicate}, ensure_ascii=False)
        scan = scanner.scan(caution)
        if isinstance(scan, ScanSuccess) and not _scan_blocks_retrieval(scan, policy):
            if caution not in cautions:
                cautions.append(caution)
    return tuple(cautions)

def _include_current_self_reports(
    ranked: tuple[_VerifiedCandidate, ...], *, character: str, policy: MemoryPolicy,
    scanner: PrivacyScanner, approved_repository: MemoryReadRepository, now: datetime,
) -> tuple[_VerifiedCandidate, ...]:
    # 過去の自己申告だけが類似検索に当たっても、同じ属性の現在値を取り落とさない。
    historical_keys = {
        (item.memory.proposition.subject, item.memory.proposition.predicate)
        for item in ranked
        if isinstance(item.memory, SemanticMemoryView)
        and item.memory.proposition is not None and item.memory.proposition.self_report
        and not item.memory.current_self_report
    }
    if not historical_keys:
        return ranked
    snapshots: list[ReadableMemory] = [
        memory for memory in approved_repository.list_active(character_id=character)
        if isinstance(memory, SemanticMemoryView) and memory.current_self_report
        and memory.proposition is not None
        and (memory.proposition.subject, memory.proposition.predicate) in historical_keys
    ]
    current = _verified_period_memories(
        snapshots, character=character, policy=policy, scanner=scanner,
        approved_repository=approved_repository, now=now,
    )
    by_id = {item.memory.id: item for item in ranked}
    result: list[_VerifiedCandidate] = []
    seen: set[UUID] = set()
    for item in ranked:
        memory = item.memory
        related = [
            value for value in current
            if isinstance(value, SemanticMemoryView) and value.current_self_report
            and value.proposition is not None and isinstance(memory, SemanticMemoryView)
            and memory.proposition is not None and not memory.current_self_report
            and (value.proposition.subject, value.proposition.predicate)
            == (memory.proposition.subject, memory.proposition.predicate)
        ]
        for value in related:
            if value.id not in seen:
                # 正本から補う場合はベクトル距離を捏造しない。
                result.append(by_id.get(value.id) or _VerifiedCandidate(
                    MemorySearchCandidate(str(value.id), float("inf")), value, RetrievalMatchKind.RELATED,
                ))
                seen.add(value.id)
        if memory.id not in seen:
            result.append(item)
            seen.add(memory.id)
    return tuple(result)

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
    approved_repository: MemoryReadRepository,
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
    memories: list[ReadableMemory], query: TemporalQuery
) -> list[ReadableMemory]:
    episodic = [
        memory for memory in memories if isinstance(memory, EpisodicMemoryView)
        and memory.five_w is not None
        and matches_time(memory.five_w.when, query.start, query.end)
    ]
    memories = [memory for memory in memories if not isinstance(memory, EpisodicMemoryView)]
    if query.kind is TemporalQueryKind.SEASON:
        return episodic + [
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
    return episodic + [
        memory
        for memory in memories
        if memory.occurred_precision in allowed_precisions
    ]


def _verified_period_memories(
    memories: list[ReadableMemory],
    *,
    character: str,
    policy: MemoryPolicy,
    scanner: PrivacyScanner,
    approved_repository: MemoryReadRepository,
    now: datetime,
) -> list[ReadableMemory]:
    verified: list[ReadableMemory] = []
    for snapshot in memories:
        # 期間候補の取得後にEmbeddingを待つため、本文・出典を返却直前に再検証する。
        memory = approved_repository.get(character_id=character, memory_id=snapshot.id)
        if memory is None or not _is_retrieval_compatible(memory, character, policy, now):
            continue
        body_scan = scanner.scan(memory.normalized_text)
        if not isinstance(body_scan, (ScanFailure, ScanSuccess)):
            continue
        if not _scan_blocks_retrieval(body_scan, policy):
            verified.append(memory)
    return verified


def _rank_temporal_candidates(
    semantic: tuple[_VerifiedCandidate, ...],
    period: list[ReadableMemory],
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
        match_kind=candidate.match_kind or match_kind,
        memory_type=memory.memory_type.value,
        raw_distance=candidate.candidate.raw_distance,
        temporal_text=(render_time(memory.five_w.when)
                       if isinstance(memory, EpisodicMemoryView) and memory.five_w else None),
        content_version=memory.content_version,
        current_self_report=isinstance(memory, SemanticMemoryView) and memory.current_self_report,
    )


def _format_occurred_at(memory: ReadableMemory) -> str | None:
    if memory.occurred_at is None or memory.occurred_timezone is None:
        return None
    try:
        zone = ZoneInfo(memory.occurred_timezone)
    except ZoneInfoNotFoundError:
        return None
    return memory.occurred_at.astimezone(zone).isoformat()


def _is_retrieval_compatible(
    memory: ReadableMemory | None,
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

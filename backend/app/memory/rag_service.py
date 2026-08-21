import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx

from app.memory.chroma_store import (
    MemorySearchCandidate,
    MemorySearchResult,
    query_memories,
)
from app.memory.embedder import embed_text
from app.memory.memory_policy import MemoryPolicy, rag_service_policy
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.contracts import ApprovedMemory, MemoryStatus
from app.memory.persistence.sqlite import format_datetime
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


def retrieve_prompt_memories(
    character: str,
    user_message: str,
    policy: MemoryPolicy,
    *,
    scanner: PrivacyScanner,
    classifier: SemanticPrivacyClassifier,
    approved_repository: ApprovedMemoryRepository,
    chroma_path: Path,
) -> tuple[MemorySearchResult, ...]:
    try:
        query_scan = scanner.scan(user_message)
        if _scan_blocks_retrieval(query_scan, policy):
            return ()
        assessment = classifier.classify(user_message, QUERY_GATE)
        if (
            not isinstance(assessment, PrivacyAssessment)
            or not isinstance(assessment.classification, SemanticClassification)
            or not isinstance(assessment.reason_code, SemanticAssessmentReasonCode)
        ):
            logger.warning("Skipped RAG memory lookup: invalid semantic assessment")
            return ()
        if assessment.classification is not SemanticClassification.NOT_SENSITIVE:
            logger.warning(
                "Skipped RAG memory lookup: semantic_reason_code=%s",
                assessment.reason_code.value,
            )
            return ()
        candidates = query_memories(
            character,
            embed_text(user_message),
            n_results=rag_service_policy(policy).max_retrieved_memories,
            chroma_path=chroma_path,
        )
        return _verified_memories(
            candidates,
            character=character,
            policy=policy,
            scanner=scanner,
            approved_repository=approved_repository,
            now=datetime.now(UTC),
        )
    except RAG_OPERATION_ERRORS as exc:
        logger.warning("RAG memory lookup failed: %s", exc.__class__.__name__)
        return ()


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


def _verified_memories(
    candidates: list[MemorySearchCandidate],
    *,
    character: str,
    policy: MemoryPolicy,
    scanner: PrivacyScanner,
    approved_repository: ApprovedMemoryRepository,
    now: datetime,
) -> tuple[MemorySearchResult, ...]:
    verified: list[MemorySearchResult] = []
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
        if _scan_blocks_retrieval(body_scan, policy):
            continue
        verified.append(
            MemorySearchResult(
                memory_id=str(memory.id),
                normalized_text=memory.normalized_text,
                effective_at=format_datetime(memory.effective_at),
                memory_type=memory.memory_type.value,
                raw_distance=candidate.raw_distance,
            )
        )
    return tuple(verified)


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

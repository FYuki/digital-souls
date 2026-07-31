import logging
from collections.abc import Callable
from typing import Protocol

import httpx

from app.memory.chroma_store import MemorySearchResult, add_memory, query_memories
from app.memory.embedder import embed_text
from app.memory.memory_policy import (
    MemoryPolicy,
    contains_non_storable_memory,
    contains_sensitive_memory,
    is_long_term_memory_candidate,
    rag_service_policy,
)
from app.memory.rag_record import (
    MemoryCandidateRecord,
    create_memory_candidate_record,
)
from app.privacy.contracts import (
    PrivacyCategory,
    PrivacyScanner,
    ScanFailure,
    ScanResult,
    StorageScope,
)

logger = logging.getLogger(__name__)
RAG_OPERATION_ERRORS = (httpx.HTTPError, OSError, RuntimeError, ValueError)


class _BackgroundTaskQueue(Protocol):
    def add_task(
        self,
        func: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        ...


def _enqueue_memory_candidate(
    record: MemoryCandidateRecord,
    policy: MemoryPolicy,
    task_queue: _BackgroundTaskQueue,
) -> None:
    if is_long_term_memory_candidate(record, policy):
        task_queue.add_task(_embed_and_store, record)


def record_user_memory_candidate(
    character: str,
    user_message: str,
    policy: MemoryPolicy,
    task_queue: _BackgroundTaskQueue,
    *,
    privacy_scanner: PrivacyScanner,
) -> None:
    if not _allows_rag_storage(privacy_scanner.scan(user_message)):
        return
    if contains_non_storable_memory(user_message, policy):
        return
    record = create_memory_candidate_record(character, user_message)
    _enqueue_memory_candidate(record, policy, task_queue)


def _allows_rag_storage(result: ScanResult) -> bool:
    if isinstance(result, ScanFailure):
        return False
    for finding in result.findings:
        if finding.category is not PrivacyCategory.STORAGE_OPT_OUT:
            return False
        if finding.storage_scope in {StorageScope.RAG, StorageScope.BOTH}:
            return False
    return True


def retrieve_prompt_memories(
    character: str,
    user_message: str,
    policy: MemoryPolicy,
) -> tuple[MemorySearchResult, ...]:
    if contains_sensitive_memory(user_message, policy):
        logger.warning("Skipped RAG memory lookup for sensitive content")
        return ()
    try:
        embedding = embed_text(user_message)
        memories = query_memories(
            character,
            embedding,
            n_results=rag_service_policy(policy).max_retrieved_memories,
        )
    except RAG_OPERATION_ERRORS as exc:
        logger.warning("RAG memory lookup failed: %s", exc.__class__.__name__)
        return ()
    return tuple(memories)


def _embed_and_store(record: MemoryCandidateRecord) -> None:
    try:
        embedding = embed_text(record.content)
        add_memory(
            record.character,
            record.id,
            embedding,
            record.content,
            {
                "character": record.character,
                "role": record.role,
                "timestamp": record.timestamp,
            },
        )
    except RAG_OPERATION_ERRORS as exc:
        logger.warning(
            "RAG memory storage failed: memory_id=%s error_type=%s",
            record.id,
            exc.__class__.__name__,
        )

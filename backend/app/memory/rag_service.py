import logging
from pathlib import Path

import httpx

from app.memory.chroma_store import MemorySearchResult, query_memories
from app.memory.embedder import embed_text
from app.memory.memory_policy import (
    MemoryPolicy,
    contains_sensitive_memory,
    rag_service_policy,
)

logger = logging.getLogger(__name__)
RAG_OPERATION_ERRORS = (httpx.HTTPError, OSError, RuntimeError, ValueError)


def retrieve_prompt_memories(
    character: str,
    user_message: str,
    policy: MemoryPolicy,
    *,
    chroma_path: Path,
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
            chroma_path=chroma_path,
        )
    except RAG_OPERATION_ERRORS as exc:
        logger.warning("RAG memory lookup failed: %s", exc.__class__.__name__)
        return ()
    return tuple(memories)

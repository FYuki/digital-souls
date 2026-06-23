import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx

from app.memory.chroma_store import MemorySearchResult, add_memory, query_memories
from app.memory.conversation_log import ConversationRecord, save_message
from app.memory.embedder import embed_text
from app.memory.memory_policy import (
    MemoryPolicy,
    contains_sensitive_memory,
    is_long_term_memory_candidate,
    rag_service_policy,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
FAILED_MEMORY_LOG_PATH = DATA_DIR / "failed-memories.jsonl"

logger = logging.getLogger(__name__)
RAG_OPERATION_ERRORS = (httpx.HTTPError, OSError, RuntimeError)


class BackgroundTaskQueue(Protocol):
    def add_task(
        self,
        func: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        ...


def _enqueue_memory_candidate(
    record: ConversationRecord,
    background_tasks: BackgroundTaskQueue,
    policy: MemoryPolicy,
) -> None:
    if is_long_term_memory_candidate(record, policy):
        background_tasks.add_task(_embed_and_store, record)


def _format_augmented_prompt(
    system_prompt: str,
    memories: list[MemorySearchResult],
) -> str:
    memory_block = "\n".join(
        f"- [{memory.timestamp}] ({memory.role}) {memory.content}"
        for memory in memories
    )
    return f"{system_prompt}\n\n過去の記憶:\n{memory_block}"


def build_augmented_system_prompt(
    character: str,
    user_message: str,
    system_prompt: str,
    policy: MemoryPolicy,
) -> str:
    if contains_sensitive_memory(user_message, policy):
        logger.warning("Skipped RAG memory lookup for sensitive content")
        return system_prompt
    try:
        embedding = embed_text(user_message)
        memories = query_memories(
            character,
            embedding,
            n_results=rag_service_policy(policy).max_retrieved_memories,
        )
    except RAG_OPERATION_ERRORS as exc:
        logger.warning("RAG memory lookup failed: %s", exc.__class__.__name__)
        return system_prompt
    if not memories:
        return system_prompt
    return _format_augmented_prompt(system_prompt, memories)


def record_chat_turn(
    character: str,
    user_message: str,
    assistant_reply: str,
    background_tasks: BackgroundTaskQueue,
    policy: MemoryPolicy,
) -> None:
    user_record = save_message(character, "user", user_message)
    assistant_record = save_message(character, "assistant", assistant_reply)
    _enqueue_memory_candidate(user_record, background_tasks, policy)
    _enqueue_memory_candidate(assistant_record, background_tasks, policy)


def _failed_record_payload(record: ConversationRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "character": record.character,
        "role": record.role,
        "content": record.content,
        "timestamp": record.timestamp,
    }


def _write_failed_record(record: ConversationRecord) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with FAILED_MEMORY_LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(_failed_record_payload(record), ensure_ascii=False))
        file.write("\n")


def _embed_and_store(record: ConversationRecord) -> None:
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
        logger.warning("RAG memory storage failed: %s", exc.__class__.__name__)
        _write_failed_record(record)

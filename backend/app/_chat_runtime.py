import asyncio
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

import httpx

from app import chat_service
from app.characters import loader as _character_loader
from app.llm import router as _llm_router
from app.memory import memory_policy as _memory_policy
from app.memory import rag_service as _rag_service
from app.memory.memory_policy import resolved_memory_policy

RAG_ENABLED_ENV = "RAG_ENABLED"
RAG_ENABLED_VALUE = "true"
RAG_MEMORY_THREAD_PREFIX = "rag-memory"
DEFAULT_RAG_MEMORY_WORKERS = 4
logger = logging.getLogger(__name__)

_default_service_lock = threading.Lock()
_default_service_resolvers: list[Callable[[], "ChatService"]] = []


class MemoryTaskQueue(Protocol):
    def add_task(
        self,
        func: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        ...


class ThreadPoolMemoryTaskQueue:
    def __init__(self, executor: ThreadPoolExecutor) -> None:
        self._executor = executor
        self._futures: set[Future[object]] = set()
        self._lock = threading.Lock()

    def add_task(
        self,
        func: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        future = self._executor.submit(func, *args, **kwargs)
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(_log_task_failure)
        future.add_done_callback(self._discard_future)

    def drain(self) -> None:
        while True:
            with self._lock:
                futures = tuple(self._futures)
            if not futures:
                return
            for future in futures:
                future.result()

    def shutdown(self) -> None:
        self.drain()
        self._executor.shutdown(wait=True)

    def _discard_future(self, future: Future[object]) -> None:
        with self._lock:
            self._futures.discard(future)


def _log_task_failure(future: Future[object]) -> None:
    if future.cancelled():
        return
    exception = future.exception()
    if exception is not None:
        logger.warning("RAG background task failed: %s", exception.__class__.__name__)


@dataclass(frozen=True)
class ChatRuntimeConfig:
    rag_enabled: bool
    memory_policy: _memory_policy.MemoryPolicy | None


@dataclass(frozen=True)
class _ResolvedChatContext:
    system_prompt: str
    memory_policy: _memory_policy.MemoryPolicy | None
    memory_task_queue: MemoryTaskQueue


@dataclass(frozen=True)
class _ChatSession:
    character: str
    chat_service: "ChatService"

    def generate_reply(self, message: str) -> str:
        return self.chat_service.generate_chat_reply(self.character, message)


class ChatService:
    def __init__(
        self,
        runtime_config: ChatRuntimeConfig,
        memory_task_queue: MemoryTaskQueue,
    ) -> None:
        if runtime_config.rag_enabled and runtime_config.memory_policy is None:
            raise ValueError("memory policy is required when RAG is enabled")
        if not runtime_config.rag_enabled and runtime_config.memory_policy is not None:
            raise ValueError("memory policy must be omitted when RAG is disabled")
        self._runtime_config = runtime_config
        self._memory_task_queue = memory_task_queue

    def generate_chat_reply(
        self,
        character: str,
        message: str,
    ) -> str:
        context = _resolve_chat_context(
            character,
            self._runtime_config,
            self._memory_task_queue,
        )
        return _generate_reply(character, message, context)

    async def create_chat_session(
        self,
        character: str,
    ) -> chat_service.ChatReplySession:
        await asyncio.to_thread(_load_system_prompt, character)
        return _ChatSession(
            character=character,
            chat_service=self,
        )


def resolve_chat_runtime_config() -> ChatRuntimeConfig:
    rag_enabled = os.environ.get(RAG_ENABLED_ENV) == RAG_ENABLED_VALUE
    policy = resolved_memory_policy() if rag_enabled else None
    return ChatRuntimeConfig(
        rag_enabled=rag_enabled,
        memory_policy=policy,
    )


def create_chat_service(
    runtime_config: ChatRuntimeConfig,
    memory_task_queue: MemoryTaskQueue,
) -> ChatService:
    return ChatService(runtime_config, memory_task_queue)


def create_thread_pool_memory_task_queue(
    executor: ThreadPoolExecutor,
) -> ThreadPoolMemoryTaskQueue:
    return ThreadPoolMemoryTaskQueue(executor)


def register_default_chat_service_resolver(
    resolver: Callable[[], ChatService],
) -> None:
    with _default_service_lock:
        _default_service_resolvers.append(resolver)


def clear_default_chat_service_resolver(
    resolver: Callable[[], ChatService],
) -> None:
    with _default_service_lock:
        _default_service_resolvers.remove(resolver)


def default_chat_service() -> ChatService:
    resolver = _current_default_service_resolver()
    if resolver is not None:
        return resolver()
    raise chat_service.ChatServiceError("default ChatService resolver is not configured")


def _current_default_service_resolver() -> Callable[[], ChatService] | None:
    with _default_service_lock:
        if not _default_service_resolvers:
            return None
        return _default_service_resolvers[-1]


def _load_system_prompt(character: str) -> str:
    try:
        return _character_loader.load_personality(character)
    except FileNotFoundError as exc:
        raise chat_service.CharacterNotFoundError(character) from exc


def _resolve_chat_context(
    character: str,
    runtime_config: ChatRuntimeConfig,
    memory_task_queue: MemoryTaskQueue,
) -> _ResolvedChatContext:
    system_prompt = _load_system_prompt(character)
    if not runtime_config.rag_enabled:
        return _ResolvedChatContext(
            system_prompt=system_prompt,
            memory_policy=None,
            memory_task_queue=memory_task_queue,
        )
    return _ResolvedChatContext(
        system_prompt=system_prompt,
        memory_policy=runtime_config.memory_policy,
        memory_task_queue=memory_task_queue,
    )


def _system_prompt_for_reply(
    character: str,
    message: str,
    context: _ResolvedChatContext,
) -> str:
    if context.memory_policy is None:
        return context.system_prompt
    return _rag_service.build_augmented_system_prompt(
        character,
        message,
        context.system_prompt,
        context.memory_policy,
    )


def _call_llm(system_prompt: str, message: str) -> str:
    try:
        return _llm_router.generate_response(system_prompt, message)
    except httpx.TimeoutException as exc:
        raise chat_service.ChatTimeoutError() from exc
    except httpx.HTTPError as exc:
        raise chat_service.ChatBackendError() from exc


def _record_user_memory_candidate(
    character: str,
    message: str,
    context: _ResolvedChatContext,
) -> None:
    if context.memory_policy is None:
        return
    _rag_service.record_user_memory_candidate(
        character,
        message,
        context.memory_policy,
        context.memory_task_queue,
    )


def _generate_reply(
    character: str,
    message: str,
    context: _ResolvedChatContext,
) -> str:
    prompt = _system_prompt_for_reply(character, message, context)
    reply = _call_llm(prompt, message)
    _record_user_memory_candidate(character, message, context)
    return reply

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
from app.conversation_history.service import (
    HistoryService,
    HistorySession,
)
from app.characters import loader as _character_loader
from app.llm import router as _llm_router
from app.memory import memory_policy as _memory_policy
from app.memory import rag_service as _rag_service
from app.privacy.contracts import PrivacyScanner
from app.prompting import (
    BuiltPrompt,
    CharacterPrompt,
    CurrentUserMessage,
    MaskedHistory,
    MaskedHistoryExchange,
    PromptBuildInput,
    PromptBuilder,
    PromptInputLimitError,
    RagContext,
    RagItem,
    TokenBudget,
)
from app.prompting.token_counter import Utf8TokenEstimator

RAG_ENABLED_ENV = "RAG_ENABLED"
RAG_ENABLED_VALUE = "true"
RAG_MEMORY_THREAD_PREFIX = "rag-memory"
DEFAULT_RAG_MEMORY_WORKERS = 4
DEFAULT_PROMPT_TOKEN_BUDGET = TokenBudget(
    total=32_768,
    character=8_192,
    rag=8_192,
    history=16_384,
    current_user=8_192,
    post_history=4_096,
)
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
    privacy_scanner: PrivacyScanner | None


@dataclass(frozen=True)
class _ResolvedChatContext:
    character_prompt: CharacterPrompt
    memory_policy: _memory_policy.MemoryPolicy | None
    privacy_scanner: PrivacyScanner | None
    memory_task_queue: MemoryTaskQueue


@dataclass(frozen=True)
class _ChatSession:
    character: str
    chat_service: "ChatService"
    history_session: HistorySession

    def generate_reply(self, message: str) -> str:
        return self.chat_service._generate_chat_reply(
            self.character,
            message,
            self.history_session,
        )


class ChatService:
    def __init__(
        self,
        runtime_config: ChatRuntimeConfig,
        memory_task_queue: MemoryTaskQueue,
        conversation_history_service: HistoryService,
    ) -> None:
        if runtime_config.rag_enabled and runtime_config.memory_policy is None:
            raise ValueError("memory policy is required when RAG is enabled")
        if runtime_config.rag_enabled and runtime_config.privacy_scanner is None:
            raise ValueError("privacy scanner is required when RAG is enabled")
        if not runtime_config.rag_enabled and runtime_config.memory_policy is not None:
            raise ValueError("memory policy must be omitted when RAG is disabled")
        if not runtime_config.rag_enabled and runtime_config.privacy_scanner is not None:
            raise ValueError("privacy scanner must be omitted when RAG is disabled")
        self._runtime_config = runtime_config
        self._memory_task_queue = memory_task_queue
        self._conversation_history_service = conversation_history_service

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
        history_session = self._conversation_history_service.open_session(character)
        return _generate_recorded_reply(
            character,
            message,
            context,
            history_session,
        )

    def _generate_chat_reply(
        self,
        character: str,
        message: str,
        history_session: HistorySession,
    ) -> str:
        context = _resolve_chat_context(
            character,
            self._runtime_config,
            self._memory_task_queue,
        )
        return _generate_recorded_reply(
            character,
            message,
            context,
            history_session,
        )

    async def create_chat_session(
        self,
        character: str,
    ) -> chat_service.ChatReplySession:
        await asyncio.to_thread(_load_character_prompt, character)
        return _ChatSession(
            character=character,
            chat_service=self,
            history_session=self._conversation_history_service.open_session(character),
        )


def resolve_chat_runtime_config(
    policy: _memory_policy.MemoryPolicy,
    privacy_scanner: PrivacyScanner,
) -> ChatRuntimeConfig:
    rag_enabled = os.environ.get(RAG_ENABLED_ENV) == RAG_ENABLED_VALUE
    return ChatRuntimeConfig(
        rag_enabled=rag_enabled,
        memory_policy=policy if rag_enabled else None,
        privacy_scanner=privacy_scanner if rag_enabled else None,
    )


def create_chat_service(
    runtime_config: ChatRuntimeConfig,
    memory_task_queue: MemoryTaskQueue,
    conversation_history_service: HistoryService,
) -> ChatService:
    return ChatService(
        runtime_config,
        memory_task_queue,
        conversation_history_service,
    )


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


def _load_character_prompt(character: str) -> CharacterPrompt:
    try:
        return _character_loader.load_character_card(
            character
        ).to_character_prompt()
    except FileNotFoundError as exc:
        raise chat_service.CharacterNotFoundError(character) from exc


def _resolve_chat_context(
    character: str,
    runtime_config: ChatRuntimeConfig,
    memory_task_queue: MemoryTaskQueue,
) -> _ResolvedChatContext:
    character_prompt = _load_character_prompt(character)
    if not runtime_config.rag_enabled:
        return _ResolvedChatContext(
            character_prompt=character_prompt,
            memory_policy=None,
            privacy_scanner=None,
            memory_task_queue=memory_task_queue,
        )
    return _ResolvedChatContext(
        character_prompt=character_prompt,
        memory_policy=runtime_config.memory_policy,
        privacy_scanner=runtime_config.privacy_scanner,
        memory_task_queue=memory_task_queue,
    )


def _rag_context_for_reply(
    character: str,
    message: str,
    context: _ResolvedChatContext,
) -> RagContext:
    if context.memory_policy is None:
        return RagContext(items=())
    memories = _rag_service.retrieve_prompt_memories(
        character,
        message,
        context.memory_policy,
    )
    return RagContext(
        items=tuple(
            RagItem(
                f"[{memory.timestamp}] ({memory.role}) {memory.content}"
            )
            for memory in memories
        )
    )


def _call_llm(prompt: BuiltPrompt) -> str:
    try:
        reply = _llm_router.generate_response(prompt)
    except httpx.TimeoutException as exc:
        raise chat_service.ChatTimeoutError() from exc
    except httpx.HTTPError as exc:
        raise chat_service.ChatBackendError() from exc
    if not reply:
        raise chat_service.ChatBackendError()
    return reply


def _record_user_memory_candidate(
    character: str,
    message: str,
    context: _ResolvedChatContext,
) -> None:
    if context.memory_policy is None:
        return
    if context.privacy_scanner is None:
        raise ValueError("privacy scanner is required for RAG memory recording")
    _rag_service.record_user_memory_candidate(
        character,
        message,
        context.memory_policy,
        context.memory_task_queue,
        privacy_scanner=context.privacy_scanner,
    )


def _generate_reply(
    character: str,
    message: str,
    context: _ResolvedChatContext,
    history_session: HistorySession,
) -> str:
    prompt = _build_prompt(character, message, context, history_session)
    reply = _call_llm(prompt)
    _record_user_memory_candidate(character, message, context)
    return reply


def _build_prompt(
    character: str,
    message: str,
    context: _ResolvedChatContext,
    history_session: HistorySession,
) -> BuiltPrompt:
    completed_exchanges = history_session.completed_exchanges()
    history = MaskedHistory(
        exchanges=tuple(
            MaskedHistoryExchange(
                user_content=exchange.user_content,
                assistant_content=exchange.assistant_content,
            )
            for exchange in completed_exchanges
        )
    )
    prompt_input = PromptBuildInput(
        character=context.character_prompt,
        rag=_rag_context_for_reply(character, message, context),
        history=history,
        current_user=CurrentUserMessage(message),
        budget=DEFAULT_PROMPT_TOKEN_BUDGET,
    )
    try:
        return PromptBuilder(Utf8TokenEstimator()).build(prompt_input)
    except PromptInputLimitError as exc:
        raise chat_service.ChatInputLimitError(
            region=exc.region,
            used=exc.used,
            limit=exc.limit,
        ) from exc


def _generate_recorded_reply(
    character: str,
    message: str,
    context: _ResolvedChatContext,
    history_session: HistorySession,
) -> str:
    started_turn = history_session.start_turn(message)
    try:
        reply = _generate_reply(
            character,
            message,
            context,
            history_session,
        )
        history_session.complete_turn(started_turn, reply)
    except Exception:
        try:
            history_session.fail_turn(started_turn)
        except Exception as cleanup_error:
            logger.warning(
                "Failed to mark conversation turn failed: %s",
                cleanup_error.__class__.__name__,
            )
        raise
    return reply

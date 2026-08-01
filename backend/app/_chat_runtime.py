import asyncio
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx

from app import chat_service
from app.conversation_history.service import (
    HistoryService,
    HistorySession,
    StartedHistoryTurn,
)
from app.memory import memory_policy as _memory_policy
from app.model_settings import ModelSettings
from app.memory import rag_service as _rag_service
from app.privacy.contracts import PrivacyScanner
from app.prompting import (
    BuiltPrompt,
    CharacterPrompt,
    CurrentUserMessage,
    PromptMessage,
    RagContext,
    RagItem,
    TokenCounter,
)

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


class CharacterPromptLoader(Protocol):
    def __call__(self, character: str) -> CharacterPrompt:
        ...


class ChatPromptBuilder(Protocol):
    def __call__(
        self,
        *,
        character: CharacterPrompt,
        rag: RagContext,
        current_user: CurrentUserMessage,
        history_session: HistorySession,
        config: ModelSettings,
        token_counter: TokenCounter,
    ) -> BuiltPrompt:
        ...


class LlmResponseGenerator(Protocol):
    def __call__(
        self,
        prompt: BuiltPrompt,
        *,
        max_output_tokens: int,
    ) -> str:
        ...


class InputTokenCounter(Protocol):
    def __call__(self, messages: tuple[PromptMessage, ...]) -> int:
        ...


@dataclass(frozen=True)
class ChatRuntimeDependencies:
    character_prompt_loader: CharacterPromptLoader
    prompt_builder: ChatPromptBuilder
    llm_response_generator: LlmResponseGenerator
    input_token_counter: InputTokenCounter


@dataclass(frozen=True)
class _ChatTokenCounter:
    count_tokens: InputTokenCounter

    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        return self.count_tokens(messages)


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
    prompt_config: ModelSettings


@dataclass(frozen=True)
class _ResolvedChatContext:
    character_prompt: CharacterPrompt
    memory_policy: _memory_policy.MemoryPolicy | None
    privacy_scanner: PrivacyScanner | None
    memory_task_queue: MemoryTaskQueue
    prompt_config: ModelSettings


@dataclass
class _ChatSession:
    character: str
    _service: "ChatService"
    history_session: HistorySession
    _pending_turns: dict[UUID, StartedHistoryTurn]
    _lock: threading.Lock
    _closed: bool

    def generate_reply(self, message: str) -> chat_service.ChatReply:
        reply, delivery_turn = self._service._generate_chat_reply(
            self.character,
            message,
            self.history_session,
        )
        fail_after_close = False
        with self._lock:
            if self._closed:
                fail_after_close = True
            elif delivery_turn is not None:
                self._pending_turns[reply.turn_id] = delivery_turn
        if fail_after_close and delivery_turn is not None:
            self.history_session.fail_turn(delivery_turn)
        return reply

    def mark_delivered(self, turn_id: UUID) -> None:
        with self._lock:
            self._pending_turns.pop(turn_id, None)

    def mark_delivery_failed(self, turn_id: UUID) -> None:
        with self._lock:
            started_turn = self._pending_turns.pop(turn_id, None)
        if started_turn is not None:
            self.history_session.fail_turn(started_turn)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._pending_turns.values())
            self._pending_turns.clear()
        for started_turn in pending:
            self.history_session.fail_turn(started_turn)


class ChatService:
    def __init__(
        self,
        runtime_config: ChatRuntimeConfig,
        memory_task_queue: MemoryTaskQueue,
        conversation_history_service: HistoryService,
        dependencies: ChatRuntimeDependencies,
    ) -> None:
        if runtime_config.rag_enabled and runtime_config.memory_policy is None:
            raise ValueError("memory policy is required when RAG is enabled")
        if runtime_config.rag_enabled and runtime_config.privacy_scanner is None:
            raise ValueError("privacy scanner is required when RAG is enabled")
        if not runtime_config.rag_enabled and runtime_config.memory_policy is not None:
            raise ValueError("memory policy must be omitted when RAG is disabled")
        if (
            not runtime_config.rag_enabled
            and runtime_config.privacy_scanner is not None
        ):
            raise ValueError("privacy scanner must be omitted when RAG is disabled")
        self._runtime_config = runtime_config
        self._memory_task_queue = memory_task_queue
        self._conversation_history_service = conversation_history_service
        self._dependencies = dependencies

    def generate_chat_reply(
        self,
        character: str,
        conversation_id: UUID,
        message: str,
    ) -> chat_service.ChatReply:
        context = _resolve_chat_context(
            character,
            self._runtime_config,
            self._memory_task_queue,
            self._dependencies,
        )
        history_session = self._conversation_history_service.open_session(
            character,
            conversation_id,
        )
        reply, _ = _generate_recorded_reply(
            character,
            message,
            context,
            history_session,
            self._dependencies,
        )
        return reply

    def _generate_chat_reply(
        self,
        character: str,
        message: str,
        history_session: HistorySession,
    ) -> tuple[chat_service.ChatReply, StartedHistoryTurn | None]:
        context = _resolve_chat_context(
            character,
            self._runtime_config,
            self._memory_task_queue,
            self._dependencies,
        )
        return _generate_recorded_reply(
            character,
            message,
            context,
            history_session,
            self._dependencies,
        )

    async def create_chat_session(
        self,
        character: str,
        conversation_id: UUID,
    ) -> chat_service.ChatReplySession:
        await asyncio.to_thread(
            _load_character_prompt,
            character,
            self._dependencies.character_prompt_loader,
        )
        return _ChatSession(
            character=character,
            _service=self,
            history_session=self._conversation_history_service.open_session(
                character,
                conversation_id,
            ),
            _pending_turns={},
            _lock=threading.Lock(),
            _closed=False,
        )


def resolve_chat_runtime_config(
    policy: _memory_policy.MemoryPolicy,
    privacy_scanner: PrivacyScanner,
    prompt_config: ModelSettings,
) -> ChatRuntimeConfig:
    rag_enabled = os.environ.get(RAG_ENABLED_ENV) == RAG_ENABLED_VALUE
    return ChatRuntimeConfig(
        rag_enabled=rag_enabled,
        memory_policy=policy if rag_enabled else None,
        privacy_scanner=privacy_scanner if rag_enabled else None,
        prompt_config=prompt_config,
    )


def create_chat_service(
    runtime_config: ChatRuntimeConfig,
    memory_task_queue: MemoryTaskQueue,
    conversation_history_service: HistoryService,
    dependencies: ChatRuntimeDependencies,
) -> ChatService:
    return ChatService(
        runtime_config,
        memory_task_queue,
        conversation_history_service,
        dependencies,
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
    raise chat_service.ChatServiceError(
        "default ChatService resolver is not configured"
    )


def _current_default_service_resolver() -> Callable[[], ChatService] | None:
    with _default_service_lock:
        if not _default_service_resolvers:
            return None
        return _default_service_resolvers[-1]


def _load_character_prompt(
    character: str,
    loader: CharacterPromptLoader,
) -> CharacterPrompt:
    try:
        return loader(character)
    except FileNotFoundError as exc:
        raise chat_service.CharacterNotFoundError(character) from exc


def _resolve_chat_context(
    character: str,
    runtime_config: ChatRuntimeConfig,
    memory_task_queue: MemoryTaskQueue,
    dependencies: ChatRuntimeDependencies,
) -> _ResolvedChatContext:
    character_prompt = _load_character_prompt(
        character,
        dependencies.character_prompt_loader,
    )
    if not runtime_config.rag_enabled:
        return _ResolvedChatContext(
            character_prompt=character_prompt,
            memory_policy=None,
            privacy_scanner=None,
            memory_task_queue=memory_task_queue,
            prompt_config=runtime_config.prompt_config,
        )
    return _ResolvedChatContext(
        character_prompt=character_prompt,
        memory_policy=runtime_config.memory_policy,
        privacy_scanner=runtime_config.privacy_scanner,
        memory_task_queue=memory_task_queue,
        prompt_config=runtime_config.prompt_config,
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


def _call_llm(
    prompt: BuiltPrompt,
    max_output_tokens: int,
    generator: LlmResponseGenerator,
) -> str:
    try:
        reply = generator(
            prompt,
            max_output_tokens=max_output_tokens,
        )
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
    dependencies: ChatRuntimeDependencies,
) -> str:
    try:
        prompt = dependencies.prompt_builder(
            character=context.character_prompt,
            rag=_rag_context_for_reply(character, message, context),
            current_user=CurrentUserMessage(message),
            history_session=history_session,
            config=context.prompt_config,
            token_counter=_ChatTokenCounter(dependencies.input_token_counter),
        )
    except httpx.TimeoutException as exc:
        raise chat_service.ChatTimeoutError() from exc
    except httpx.HTTPError as exc:
        raise chat_service.ChatBackendError() from exc
    reply = _call_llm(
        prompt,
        context.prompt_config.assistant_max_generation_tokens,
        dependencies.llm_response_generator,
    )
    return reply


def _generate_recorded_reply(
    character: str,
    message: str,
    context: _ResolvedChatContext,
    history_session: HistorySession,
    dependencies: ChatRuntimeDependencies,
) -> tuple[chat_service.ChatReply, StartedHistoryTurn | None]:
    started_turn = history_session.start_turn(message)
    try:
        reply = _generate_reply(
            character,
            message,
            context,
            history_session,
            dependencies,
        )
        delivery_trackable = history_session.complete_turn(started_turn, reply)
        _record_user_memory_candidate(character, message, context)
    except Exception:
        try:
            history_session.fail_turn(started_turn)
        except Exception as cleanup_error:
            logger.warning(
                "Failed to mark conversation turn failed: %s",
                cleanup_error.__class__.__name__,
            )
        raise
    delivery_turn = started_turn if delivery_trackable else None
    return chat_service.ChatReply(reply, started_turn.turn_id), delivery_turn

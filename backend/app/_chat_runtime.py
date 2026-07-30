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
from app.characters import loader as _character_loader
from app.characters.models import CharacterCardData
from app.conversation_history.errors import ConversationNotFoundError
from app.conversation_history.models import (
    Conversation,
    ConversationTurn,
    PersistedMaskedText,
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
    TurnStatus,
)
from app.conversation_history.sanitizer import (
    ConversationHistorySanitizer,
    SanitizedContent,
)
from app.conversation_history.scan_models import ScanResult
from app.llm import router as _llm_router
from app.memory import memory_policy as _memory_policy
from app.memory import rag_service as _rag_service
from app.memory.memory_policy import resolved_memory_policy
from app.prompting.builder import PromptBuilder
from app.prompting.types import (
    BuiltPrompt,
    CurrentUserOriginalText,
    PersistedConversationMessage,
    PromptTokenBudget,
    RagContextText,
)

RAG_ENABLED_ENV = "RAG_ENABLED"
RAG_ENABLED_VALUE = "true"
RAG_MEMORY_THREAD_PREFIX = "rag-memory"
DEFAULT_RAG_MEMORY_WORKERS = 4
DEFAULT_PROMPT_TOKEN_BUDGET = PromptTokenBudget(
    character_and_system=2048,
    rag=1024,
    history=2048,
    current_user=1024,
    final_instructions=512,
    total=8192,
)
logger = logging.getLogger(__name__)

class MemoryTaskQueue(Protocol):
    def add_task(
        self,
        func: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        ...


class _DefaultChatService(Protocol):
    def generate_chat_reply(self, character: str, message: str) -> str:
        ...

    async def create_chat_session(
        self,
        character: str,
    ) -> chat_service.ChatReplySession:
        ...


_default_service_lock = threading.Lock()
_default_service_resolvers: list[Callable[[], _DefaultChatService]] = []


class ConversationHistoryRepository(Protocol):
    def create_conversation(self, character_id: str) -> Conversation:
        ...

    def resume_conversation(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> Conversation:
        ...

    def list_turns(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> list[ConversationTurn]:
        ...

    def create_processing_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_input: ProcessingTurnInput,
    ) -> ConversationTurn:
        ...

    def complete_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        sanitized_assistant_content: PersistedMaskedText,
    ) -> ConversationTurn:
        ...

    def create_privacy_skipped_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_input: PrivacySkippedTurnInput,
    ) -> ConversationTurn:
        ...

    def privacy_skip_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
        turn_input: PrivacySkippedTurnInput,
    ) -> ConversationTurn:
        ...

    def fail_turn(
        self,
        character_id: str,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> ConversationTurn:
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
    character_card: CharacterCardData
    memory_policy: _memory_policy.MemoryPolicy | None
    memory_task_queue: MemoryTaskQueue
    conversation_history: ConversationHistoryRepository
    history_sanitizer: ConversationHistorySanitizer
    conversation_id: UUID


@dataclass(frozen=True)
class _ChatSession:
    character: str
    conversation_id: UUID
    initial_assistant_message: str | None
    chat_service: "ChatService"

    def generate_reply(self, message: str) -> str:
        return self.chat_service.generate_conversation_reply(
            self.character,
            self.conversation_id,
            message,
        )


@dataclass(frozen=True)
class ChatReply:
    conversation_id: UUID
    response: str


class ChatService:
    def __init__(
        self,
        runtime_config: ChatRuntimeConfig,
        memory_task_queue: MemoryTaskQueue,
        conversation_history: ConversationHistoryRepository,
        history_sanitizer: ConversationHistorySanitizer,
    ) -> None:
        if runtime_config.rag_enabled and runtime_config.memory_policy is None:
            raise ValueError("memory policy is required when RAG is enabled")
        if not runtime_config.rag_enabled and runtime_config.memory_policy is not None:
            raise ValueError("memory policy must be omitted when RAG is disabled")
        self._runtime_config = runtime_config
        self._memory_task_queue = memory_task_queue
        self._conversation_history = conversation_history
        self._history_sanitizer = history_sanitizer

    def generate_chat_reply(
        self,
        character: str,
        message: str,
    ) -> str:
        return self.generate_http_reply(character, message, None).response

    def generate_http_reply(
        self,
        character: str,
        message: str,
        conversation_id: UUID | None,
    ) -> ChatReply:
        character_card = _load_character_card(character)
        conversation = self._resolve_http_conversation(character, conversation_id)
        context = _resolve_chat_context_with_card(
            character_card,
            conversation.conversation_id,
            self._runtime_config,
            self._memory_task_queue,
            self._conversation_history,
            self._history_sanitizer,
        )
        return ChatReply(
            conversation_id=conversation.conversation_id,
            response=_generate_reply(
                character,
                message,
                context,
            ),
        )

    def _resolve_http_conversation(
        self,
        character: str,
        conversation_id: UUID | None,
    ) -> Conversation:
        if conversation_id is None:
            return self._conversation_history.create_conversation(character)
        try:
            return self._conversation_history.resume_conversation(
                character,
                conversation_id,
            )
        except ConversationNotFoundError as exc:
            raise chat_service.ChatConversationNotFoundError() from exc

    def generate_conversation_reply(
        self,
        character: str,
        conversation_id: UUID,
        message: str,
    ) -> str:
        context = _resolve_chat_context(
            character,
            conversation_id,
            self._runtime_config,
            self._memory_task_queue,
            self._conversation_history,
            self._history_sanitizer,
        )
        return _generate_reply(character, message, context)

    async def create_chat_session(
        self,
        character: str,
    ) -> chat_service.ChatReplySession:
        character_card = await asyncio.to_thread(_load_character_card, character)
        conversation = await asyncio.to_thread(
            self._conversation_history.create_conversation,
            character,
        )
        return _ChatSession(
            character=character,
            conversation_id=conversation.conversation_id,
            initial_assistant_message=_initial_assistant_message(character_card),
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
    conversation_history: ConversationHistoryRepository,
    history_sanitizer: ConversationHistorySanitizer,
) -> ChatService:
    return ChatService(
        runtime_config,
        memory_task_queue,
        conversation_history,
        history_sanitizer,
    )


def create_thread_pool_memory_task_queue(
    executor: ThreadPoolExecutor,
) -> ThreadPoolMemoryTaskQueue:
    return ThreadPoolMemoryTaskQueue(executor)


def register_default_chat_service_resolver(
    resolver: Callable[[], _DefaultChatService],
) -> None:
    with _default_service_lock:
        _default_service_resolvers.append(resolver)


def clear_default_chat_service_resolver(
    resolver: Callable[[], _DefaultChatService],
) -> None:
    with _default_service_lock:
        _default_service_resolvers.remove(resolver)


def default_chat_service() -> _DefaultChatService:
    resolver = _current_default_service_resolver()
    if resolver is not None:
        return resolver()
    raise chat_service.ChatServiceError("default ChatService resolver is not configured")


def _current_default_service_resolver() -> (
    Callable[[], _DefaultChatService] | None
):
    with _default_service_lock:
        if not _default_service_resolvers:
            return None
        return _default_service_resolvers[-1]


def _load_character_card(character: str) -> CharacterCardData:
    try:
        return _character_loader.load_character_card(character).data
    except FileNotFoundError as exc:
        raise chat_service.CharacterNotFoundError(character) from exc


def _initial_assistant_message(character_card: CharacterCardData) -> str | None:
    if not character_card.first_mes.strip():
        return None
    return character_card.first_mes


def _resolve_chat_context(
    character: str,
    conversation_id: UUID,
    runtime_config: ChatRuntimeConfig,
    memory_task_queue: MemoryTaskQueue,
    conversation_history: ConversationHistoryRepository,
    history_sanitizer: ConversationHistorySanitizer,
) -> _ResolvedChatContext:
    character_card = _load_character_card(character)
    return _resolve_chat_context_with_card(
        character_card,
        conversation_id,
        runtime_config,
        memory_task_queue,
        conversation_history,
        history_sanitizer,
    )


def _resolve_chat_context_with_card(
    character_card: CharacterCardData,
    conversation_id: UUID,
    runtime_config: ChatRuntimeConfig,
    memory_task_queue: MemoryTaskQueue,
    conversation_history: ConversationHistoryRepository,
    history_sanitizer: ConversationHistorySanitizer,
) -> _ResolvedChatContext:
    if not runtime_config.rag_enabled:
        return _ResolvedChatContext(
            character_card=character_card,
            memory_policy=None,
            memory_task_queue=memory_task_queue,
            conversation_history=conversation_history,
            history_sanitizer=history_sanitizer,
            conversation_id=conversation_id,
        )
    return _ResolvedChatContext(
        character_card=character_card,
        memory_policy=runtime_config.memory_policy,
        memory_task_queue=memory_task_queue,
        conversation_history=conversation_history,
        history_sanitizer=history_sanitizer,
        conversation_id=conversation_id,
    )


def _rag_context_for_reply(
    character: str,
    message: str,
    context: _ResolvedChatContext,
    user_scan_result: ScanResult,
) -> tuple[RagContextText, ...]:
    if context.memory_policy is None:
        return ()
    rag_contexts = _rag_service.build_rag_context_for_scanned_user(
        character,
        message,
        context.memory_policy,
        user_scan_result,
        context.history_sanitizer.scanner,
    )
    return tuple(
        RagContextText(rag_context.strip())
        for rag_context in rag_contexts
        if rag_context.strip()
    )


def _build_prompt(
    character: str,
    message: str,
    context: _ResolvedChatContext,
    user_scan_result: ScanResult,
) -> BuiltPrompt:
    persisted_history = _load_persisted_history(
        character,
        context.conversation_id,
        context.conversation_history,
    )
    return PromptBuilder().build(
        character=context.character_card,
        rag_context=_rag_context_for_reply(
            character,
            message,
            context,
            user_scan_result,
        ),
        persisted_history=persisted_history,
        current_user_original_text=CurrentUserOriginalText(message),
        post_history_instructions=context.character_card.post_history_instructions,
        token_budget=DEFAULT_PROMPT_TOKEN_BUDGET,
    )


def _load_persisted_history(
    character: str,
    conversation_id: UUID,
    repository: ConversationHistoryRepository,
) -> tuple[PersistedConversationMessage, ...]:
    messages: list[PersistedConversationMessage] = []
    for turn in repository.list_turns(character, conversation_id):
        if turn.status is not TurnStatus.COMPLETED:
            continue
        if turn.user_content is None or turn.assistant_content is None:
            raise ValueError("completed conversation turn must contain both messages")
        messages.extend(
            (
                PersistedConversationMessage(
                    role="user",
                    content=turn.user_content,
                ),
                PersistedConversationMessage(
                    role="assistant",
                    content=turn.assistant_content,
                ),
            )
        )
    return tuple(messages)


def _call_llm(prompt: BuiltPrompt) -> str:
    try:
        return _llm_router.generate_response(prompt)
    except httpx.TimeoutException as exc:
        raise chat_service.ChatTimeoutError() from exc
    except httpx.HTTPError as exc:
        raise chat_service.ChatBackendError() from exc


def _record_user_memory_candidate(
    character: str,
    message: str,
    context: _ResolvedChatContext,
    user_scan_result: ScanResult,
) -> None:
    if context.memory_policy is None:
        return
    _rag_service.record_scanned_user_memory_candidate(
        character,
        message,
        context.memory_policy,
        context.memory_task_queue,
        user_scan_result,
    )


def _generate_reply(
    character: str,
    message: str,
    context: _ResolvedChatContext,
) -> str:
    user_scan_result = context.history_sanitizer.scanner.scan(message)
    user_decision = context.history_sanitizer.decide_user_content(
        message,
        user_scan_result,
    )
    if isinstance(user_decision, SanitizedContent):
        turn = context.conversation_history.create_processing_turn(
            character,
            context.conversation_id,
            ProcessingTurnInput(sanitized_user_content=user_decision.content),
        )
    else:
        context.conversation_history.create_privacy_skipped_turn(
            character,
            context.conversation_id,
            PrivacySkippedTurnInput(reason_code=user_decision.reason_code),
        )
        reply = _call_llm(
            _build_prompt(character, message, context, user_scan_result)
        )
        _record_user_memory_candidate(
            character,
            message,
            context,
            user_scan_result,
        )
        return reply
    try:
        prompt = _build_prompt(character, message, context, user_scan_result)
        reply = _call_llm(prompt)
        assistant_decision = context.history_sanitizer.sanitize_assistant_content(reply)
        if isinstance(assistant_decision, SanitizedContent):
            context.conversation_history.complete_turn(
                character,
                context.conversation_id,
                turn.turn_id,
                sanitized_assistant_content=assistant_decision.content,
            )
        else:
            context.conversation_history.privacy_skip_turn(
                character,
                context.conversation_id,
                turn.turn_id,
                PrivacySkippedTurnInput(
                    reason_code=assistant_decision.reason_code,
                ),
            )
    except Exception:
        context.conversation_history.fail_turn(
            character,
            context.conversation_id,
            turn.turn_id,
        )
        raise
    _record_user_memory_candidate(
        character,
        message,
        context,
        user_scan_result,
    )
    return reply

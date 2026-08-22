import asyncio
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx

from app import chat_service
from app.conversation_history.models import ConversationTurn, TurnStatus
from app.conversation_history.service import (
    HistoryService,
    HistorySession,
    StartedHistoryTurn,
)
from app.memory import memory_policy as _memory_policy
from app.model_settings import ModelSettings
from app.runtime_paths import RuntimePaths
from app.memory import rag_service as _rag_service
from app.memory.chroma_store import MemorySearchResult
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.sqlite import format_datetime
from app.memory.formation.contracts import MemoryFormationJob
from app.prompting import (
    BuiltPrompt,
    CharacterPrompt,
    CurrentUserMessage,
    PromptMessage,
    PromptMemoryReference,
    RagContext,
    RagItem,
    TokenCounter,
)
from app.privacy.contracts import PrivacyScanner
from app.privacy.semantic.classifier import SemanticPrivacyClassifier

RAG_ENABLED_ENV = "RAG_ENABLED"
RAG_ENABLED_VALUE = "true"
logger = logging.getLogger(__name__)

_default_service_lock = threading.Lock()
_default_service_resolvers: list[Callable[[], "ChatService"]] = []


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


class MemoryFormationSubmitter(Protocol):
    def submit(self, job: MemoryFormationJob) -> None: ...


@dataclass(frozen=True)
class ChatRuntimeDependencies:
    character_prompt_loader: CharacterPromptLoader
    prompt_builder: ChatPromptBuilder
    llm_response_generator: LlmResponseGenerator
    input_token_counter: InputTokenCounter
    privacy_scanner: PrivacyScanner
    semantic_classifier: SemanticPrivacyClassifier
    approved_memory_repository: ApprovedMemoryRepository
    memory_formation_submitter: MemoryFormationSubmitter
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class _ChatTokenCounter:
    count_tokens: InputTokenCounter

    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        return self.count_tokens(messages)


@dataclass(frozen=True)
class ChatRuntimeConfig:
    rag_enabled: bool
    memory_policy: _memory_policy.MemoryPolicy | None
    prompt_config: ModelSettings
    chroma_path: Path
    occurred_timezone: str = "Asia/Tokyo"


@dataclass(frozen=True)
class _ResolvedChatContext:
    character_prompt: CharacterPrompt
    memory_policy: _memory_policy.MemoryPolicy | None
    prompt_config: ModelSettings
    chroma_path: Path
    occurred_timezone: str


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
        conversation_history_service: HistoryService,
        dependencies: ChatRuntimeDependencies,
    ) -> None:
        if runtime_config.rag_enabled and runtime_config.memory_policy is None:
            raise ValueError("memory policy is required when RAG is enabled")
        if not runtime_config.rag_enabled and runtime_config.memory_policy is not None:
            raise ValueError("memory policy must be omitted when RAG is disabled")
        self._runtime_config = runtime_config
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
    prompt_config: ModelSettings,
    runtime_paths: RuntimePaths,
    occurred_timezone: str = "Asia/Tokyo",
) -> ChatRuntimeConfig:
    rag_enabled = os.environ.get(RAG_ENABLED_ENV) == RAG_ENABLED_VALUE
    return ChatRuntimeConfig(
        rag_enabled=rag_enabled,
        memory_policy=policy if rag_enabled else None,
        prompt_config=prompt_config,
        chroma_path=runtime_paths.chroma_path,
        occurred_timezone=occurred_timezone,
    )


def create_chat_service(
    runtime_config: ChatRuntimeConfig,
    conversation_history_service: HistoryService,
    dependencies: ChatRuntimeDependencies,
) -> ChatService:
    return ChatService(
        runtime_config,
        conversation_history_service,
        dependencies,
    )


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
            prompt_config=runtime_config.prompt_config,
            chroma_path=runtime_config.chroma_path,
            occurred_timezone=runtime_config.occurred_timezone,
        )
    return _ResolvedChatContext(
        character_prompt=character_prompt,
        memory_policy=runtime_config.memory_policy,
        prompt_config=runtime_config.prompt_config,
        chroma_path=runtime_config.chroma_path,
        occurred_timezone=runtime_config.occurred_timezone,
    )


def _rag_context_for_reply(
    character: str,
    message: str,
    context: _ResolvedChatContext,
    dependencies: ChatRuntimeDependencies,
) -> RagContext:
    if context.memory_policy is None:
        return RagContext(items=())
    outcome = _rag_service.retrieve_prompt_memories(
        character,
        message,
        context.memory_policy,
        scanner=dependencies.privacy_scanner,
        classifier=dependencies.semantic_classifier,
        approved_repository=dependencies.approved_memory_repository,
        chroma_path=context.chroma_path,
        now=dependencies.clock(),
        timezone=context.occurred_timezone,
    )
    if outcome.no_match:
        return RagContext(
            items=(
                RagItem(
                    "指定された期間に該当する記憶はありません。"
                    "推測で補完しないでください。",
                    raw_distance=0.0,
                ),
            )
        )
    return RagContext(
        items=tuple(
            RagItem(
                _memory_prompt_content(memory),
                raw_distance=memory.raw_distance,
                reference=PromptMemoryReference(
                    memory_id=memory.memory_id,
                    occurred_at=(
                        None
                        if memory.occurred_at is None
                        else datetime.fromisoformat(memory.occurred_at)
                    ),
                    occurred_precision=(
                        None
                        if memory.occurred_precision is None
                        else memory.occurred_precision.value
                    ),
                    match_kind=memory.match_kind.value,
                ),
            )
            for memory in outcome.memories
        )
    )


def _memory_prompt_content(memory: MemorySearchResult) -> str:
    if memory.occurred_at is None:
        return memory.normalized_text
    precision = (
        ""
        if memory.occurred_precision is None
        else f" {memory.occurred_precision.value}"
    )
    return f"[{memory.occurred_at}{precision}] {memory.normalized_text}"


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
            rag=_rag_context_for_reply(character, message, context, dependencies),
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
    references = tuple(
        message.memory_reference
        for message in prompt.messages
        if message.memory_reference is not None
    )
    if references:
        logger.info(
            "Prompt memories selected: references=%s",
            tuple(
                {
                    "memory_id": reference.memory_id,
                    "occurred_at": (
                        None
                        if reference.occurred_at is None
                        else format_datetime(reference.occurred_at)
                    ),
                    "occurred_precision": reference.occurred_precision,
                    "match_kind": reference.match_kind,
                }
                for reference in references
            ),
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
        persisted_turn = history_session.complete_turn(started_turn, reply)
    except Exception:
        try:
            history_session.fail_turn(started_turn)
        except Exception as cleanup_error:
            logger.warning(
                "Failed to mark conversation turn failed: %s",
                cleanup_error.__class__.__name__,
            )
        raise
    if persisted_turn.status is TurnStatus.COMPLETED:
        dependencies.memory_formation_submitter.submit(
            MemoryFormationJob(
                character_id=persisted_turn.character_id,
                conversation_id=persisted_turn.conversation_id,
                turn_id=persisted_turn.turn_id,
            )
        )
    delivery_turn = (
        started_turn if persisted_turn.status is TurnStatus.COMPLETED else None
    )
    return _persisted_chat_reply(persisted_turn), delivery_turn


def _persisted_chat_reply(turn: ConversationTurn) -> chat_service.ChatReply:
    persisted: chat_service.PersistedTurn
    if turn.status is TurnStatus.PRIVACY_SKIPPED:
        if (
            turn.privacy_reason_code is None
            or turn.sanitizer_version is None
            or turn.policy_version is None
        ):
            raise ValueError("privacy_skipped turn requires metadata")
        persisted = chat_service.PersistedPrivacySkippedTurn(
            turn_id=turn.turn_id,
            reason_code=turn.privacy_reason_code,
            sanitizer_version=turn.sanitizer_version,
            policy_version=turn.policy_version,
        )
        return chat_service.ChatReply(turn.turn_id, persisted)
    if turn.user_content is None or turn.assistant_content is None:
        raise ValueError("completed turn requires persisted content")
    persisted = chat_service.PersistedContentTurn(
        turn_id=turn.turn_id,
        user_content=turn.user_content,
        assistant_content=turn.assistant_content,
    )
    return chat_service.ChatReply(turn.turn_id, persisted)

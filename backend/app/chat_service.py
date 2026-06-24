import os
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
import asyncio
import logging
from typing import Protocol

import httpx

from app.characters import loader as _character_loader
from app.llm import router as _llm_router
from app.memory import memory_policy as _memory_policy
from app.memory import rag_service as _rag_service

RAG_ENABLED_ENV = "RAG_ENABLED"
RAG_ENABLED_VALUE = "true"
logger = logging.getLogger(__name__)

__all__ = [
    "CharacterNotFoundError",
    "ChatBackendError",
    "ChatReplySession",
    "ChatServiceError",
    "ChatTimeoutError",
    "create_chat_session",
    "generate_chat_reply",
]


class ChatServiceError(Exception):
    """Base error type for failures that routers convert to chat responses."""


class CharacterNotFoundError(ChatServiceError):
    def __init__(self, character: str) -> None:
        self.character = character
        self.detail = f"Character '{character}' not found"
        super().__init__(self.detail)


class ChatTimeoutError(ChatServiceError):
    def __init__(self) -> None:
        self.detail = "LLM request timed out"
        super().__init__(self.detail)


class ChatBackendError(ChatServiceError):
    def __init__(self) -> None:
        self.detail = "LLM request failed"
        super().__init__(self.detail)


class ChatReplySession(Protocol):
    def generate_reply(self, message: str) -> str:
        ...


class _ThreadPoolChatTaskQueue:
    def __init__(self, executor: ThreadPoolExecutor) -> None:
        self._executor = executor

    def add_task(
        self,
        func: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        future = self._executor.submit(partial(func, *args, **kwargs))
        future.add_done_callback(_log_task_failure)


class _EventLoopChatTaskQueue:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add_task(
        self,
        func: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> None:
        self._loop.call_soon_threadsafe(
            self._submit,
            partial(func, *args, **kwargs),
        )

    def _submit(self, task: Callable[[], object]) -> None:
        future = self._loop.run_in_executor(None, task)
        future.add_done_callback(_log_task_failure)


_DEFAULT_TASK_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="chat-memory",
)
_DEFAULT_TASK_QUEUE = _ThreadPoolChatTaskQueue(_DEFAULT_TASK_EXECUTOR)


def _log_task_failure(future: Future[object] | asyncio.Future[object]) -> None:
    if future.cancelled():
        return
    exception = future.exception()
    if exception is not None:
        logger.warning("Chat background task failed: %s", exception.__class__.__name__)


@dataclass(frozen=True)
class _ResolvedChatContext:
    system_prompt: str
    memory_policy: _memory_policy.MemoryPolicy | None


@dataclass(frozen=True)
class _ChatSession:
    character: str
    context: _ResolvedChatContext
    task_queue: _rag_service.BackgroundTaskQueue

    def generate_reply(self, message: str) -> str:
        return _generate_reply(self.character, message, self.context, self.task_queue)


def _rag_enabled() -> bool:
    return os.environ.get(RAG_ENABLED_ENV) == RAG_ENABLED_VALUE


def _load_system_prompt(character: str) -> str:
    try:
        return _character_loader.load_personality(character)
    except FileNotFoundError as exc:
        raise CharacterNotFoundError(character) from exc


def _resolve_chat_context(character: str) -> _ResolvedChatContext:
    system_prompt = _load_system_prompt(character)
    if not _rag_enabled():
        return _ResolvedChatContext(system_prompt=system_prompt, memory_policy=None)
    return _ResolvedChatContext(
        system_prompt=system_prompt,
        memory_policy=_memory_policy.resolved_memory_policy(),
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
        raise ChatTimeoutError() from exc
    except httpx.HTTPError as exc:
        raise ChatBackendError() from exc


def _record_reply(
    character: str,
    message: str,
    reply: str,
    task_queue: _rag_service.BackgroundTaskQueue,
    memory_policy: _memory_policy.MemoryPolicy | None,
) -> None:
    if memory_policy is None:
        return
    _rag_service.record_chat_turn(character, message, reply, task_queue, memory_policy)


def _generate_reply(
    character: str,
    message: str,
    context: _ResolvedChatContext,
    task_queue: _rag_service.BackgroundTaskQueue,
) -> str:
    prompt = _system_prompt_for_reply(character, message, context)
    reply = _call_llm(prompt, message)
    _record_reply(character, message, reply, task_queue, context.memory_policy)
    return reply


def generate_chat_reply(character: str, message: str) -> str:
    context = _resolve_chat_context(character)
    return _generate_reply(character, message, context, _DEFAULT_TASK_QUEUE)


async def create_chat_session(character: str) -> ChatReplySession:
    event_loop = asyncio.get_running_loop()
    context = await asyncio.to_thread(_resolve_chat_context, character)
    return _ChatSession(
        character=character,
        context=context,
        task_queue=_EventLoopChatTaskQueue(event_loop),
    )

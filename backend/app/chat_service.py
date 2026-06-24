import asyncio
import os
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.characters import loader as _character_loader
from app.llm import router as _llm_router
from app.memory import memory_policy as _memory_policy
from app.memory import rag_service as _rag_service

RAG_ENABLED_ENV = "RAG_ENABLED"
RAG_ENABLED_VALUE = "true"

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


@dataclass(frozen=True)
class _ResolvedChatContext:
    system_prompt: str
    memory_policy: _memory_policy.MemoryPolicy | None


@dataclass(frozen=True)
class _ChatSession:
    character: str
    context: _ResolvedChatContext

    def generate_reply(self, message: str) -> str:
        return _generate_reply(self.character, message, self.context)


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
    memory_policy: _memory_policy.MemoryPolicy | None,
) -> None:
    if memory_policy is None:
        return
    _rag_service.record_chat_turn(character, message, reply, memory_policy)


def _generate_reply(
    character: str,
    message: str,
    context: _ResolvedChatContext,
) -> str:
    prompt = _system_prompt_for_reply(character, message, context)
    reply = _call_llm(prompt, message)
    _record_reply(character, message, reply, context.memory_policy)
    return reply


def generate_chat_reply(character: str, message: str) -> str:
    context = _resolve_chat_context(character)
    return _generate_reply(character, message, context)


async def create_chat_session(character: str) -> ChatReplySession:
    context = await asyncio.to_thread(_resolve_chat_context, character)
    return _ChatSession(
        character=character,
        context=context,
    )

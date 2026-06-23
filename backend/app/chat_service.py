from dataclasses import dataclass

import httpx

from app.characters import loader as _character_loader
from app.llm import router as _llm_router


__all__ = [
    "CharacterNotFoundError",
    "ChatBackendError",
    "ChatServiceError",
    "ChatSession",
    "ChatTimeoutError",
    "create_chat_session",
    "generate_chat_reply",
]


class ChatServiceError(Exception):
    pass


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


@dataclass(frozen=True)
class ChatSession:
    system_prompt: str

    def generate_reply(self, message: str) -> str:
        return _generate_reply_from_prompt(self.system_prompt, message)


def create_chat_session(character: str) -> ChatSession:
    try:
        system_prompt = _character_loader.load_personality(character)
    except FileNotFoundError as exc:
        raise CharacterNotFoundError(character) from exc

    return ChatSession(system_prompt=system_prompt)


def generate_chat_reply(
    character: str,
    message: str,
) -> str:
    return create_chat_session(character).generate_reply(message)


def _generate_reply_from_prompt(system_prompt: str, message: str) -> str:
    try:
        return _llm_router.generate_response(system_prompt, message)
    except httpx.TimeoutException as exc:
        raise ChatTimeoutError() from exc
    except httpx.HTTPError as exc:
        raise ChatBackendError() from exc

from typing import Protocol

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


def generate_chat_reply(character: str, message: str) -> str:
    from app import _chat_runtime

    return _chat_runtime.default_chat_service().generate_chat_reply(character, message)


async def create_chat_session(character: str) -> ChatReplySession:
    from app import _chat_runtime

    return await _chat_runtime.default_chat_service().create_chat_session(character)

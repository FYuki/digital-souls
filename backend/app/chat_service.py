from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

__all__ = [
    "CharacterNotFoundError",
    "ChatBackendError",
    "ChatInputLimitError",
    "ChatReply",
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


class ChatInputLimitError(ChatServiceError):
    def __init__(self, region: str, used: int, limit: int) -> None:
        self.region = region
        self.used = used
        self.limit = limit
        self.detail = (
            "Prompt input exceeds token budget: "
            f"region={region} used={used} limit={limit}"
        )
        super().__init__(self.detail)


@dataclass(frozen=True)
class ChatReply:
    response: str
    turn_id: UUID


class ChatReplySession(Protocol):
    def generate_reply(self, message: str) -> ChatReply:
        ...

    def mark_delivered(self, turn_id: UUID) -> None:
        ...

    def mark_delivery_failed(self, turn_id: UUID) -> None:
        ...

    def close(self) -> None:
        ...


def generate_chat_reply(
    character: str,
    conversation_id: UUID,
    message: str,
) -> ChatReply:
    from app import _chat_runtime

    return _chat_runtime.default_chat_service().generate_chat_reply(
        character,
        conversation_id,
        message,
    )


async def create_chat_session(
    character: str,
    conversation_id: UUID,
) -> ChatReplySession:
    from app import _chat_runtime

    return await _chat_runtime.default_chat_service().create_chat_session(
        character,
        conversation_id,
    )

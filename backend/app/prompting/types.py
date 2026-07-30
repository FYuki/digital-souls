from dataclasses import dataclass, field
from typing import ClassVar, Literal, NewType

from app.conversation_history.models import PersistedMaskedText

CurrentUserOriginalText = NewType("CurrentUserOriginalText", str)
RagContextText = NewType("RagContextText", str)
PromptRole = Literal["system", "user", "assistant"]
PersistedRole = Literal["user", "assistant"]


@dataclass(frozen=True)
class PersistedConversationMessage:
    role: PersistedRole
    content: PersistedMaskedText = field(repr=False)


@dataclass(frozen=True)
class PromptMessage:
    role: PromptRole
    content: str = field(repr=False)


@dataclass(frozen=True)
class PromptTokenBudget:
    character_and_system: int
    rag: int
    history: int
    current_user: int
    final_instructions: int
    total: int

    retention_priority: ClassVar[tuple[str, ...]] = (
        "character_and_system",
        "current_user",
        "latest_exchange",
    )
    reduction_priority: ClassVar[tuple[str, ...]] = ("rag", "oldest_history")

    def __post_init__(self) -> None:
        for field_name in (
            "character_and_system",
            "rag",
            "history",
            "current_user",
            "final_instructions",
            "total",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")


@dataclass(frozen=True)
class BuiltPrompt:
    messages: tuple[PromptMessage, ...] = field(repr=False)
    token_budget: PromptTokenBudget


class PromptInputLimitError(ValueError):
    def __init__(self, element: str, limit: int) -> None:
        self.element = element
        self.limit = limit
        super().__init__(f"{element} exceeds its token limit ({limit})")

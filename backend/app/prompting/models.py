from dataclasses import dataclass
from enum import Enum


class PromptRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, repr=False)
class CharacterPrompt:
    description: str
    personality: str
    scenario: str
    system_prompt: str
    mes_example: str
    post_history_instructions: str

    def __post_init__(self) -> None:
        values = (
            self.description,
            self.personality,
            self.scenario,
            self.system_prompt,
            self.mes_example,
            self.post_history_instructions,
        )
        if not all(isinstance(value, str) for value in values):
            raise TypeError("character prompt fields must be strings")


@dataclass(frozen=True, repr=False)
class RagItem:
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")


@dataclass(frozen=True, repr=False)
class RagContext:
    items: tuple[RagItem, ...]

    def __post_init__(self) -> None:
        if not all(isinstance(item, RagItem) for item in self.items):
            raise TypeError("items must contain only RagItem values")


@dataclass(frozen=True, repr=False)
class MaskedHistoryExchange:
    user_content: str
    assistant_content: str

    def __post_init__(self) -> None:
        if not isinstance(self.user_content, str):
            raise TypeError("user_content must be a string")
        if not isinstance(self.assistant_content, str):
            raise TypeError("assistant_content must be a string")


@dataclass(frozen=True, repr=False)
class MaskedHistory:
    exchanges: tuple[MaskedHistoryExchange, ...]

    def __post_init__(self) -> None:
        if not all(
            isinstance(exchange, MaskedHistoryExchange)
            for exchange in self.exchanges
        ):
            raise TypeError(
                "exchanges must contain only MaskedHistoryExchange values"
            )


@dataclass(frozen=True, repr=False)
class CurrentUserMessage:
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")


@dataclass(frozen=True)
class TokenBudget:
    total: int
    character: int
    rag: int
    history: int
    current_user: int
    post_history: int

    def __post_init__(self) -> None:
        values = (
            self.total,
            self.character,
            self.rag,
            self.history,
            self.current_user,
            self.post_history,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("token budget values must be non-negative integers")


@dataclass(frozen=True, repr=False)
class PromptBuildInput:
    character: CharacterPrompt
    rag: RagContext
    history: MaskedHistory
    current_user: CurrentUserMessage
    budget: TokenBudget

    def __post_init__(self) -> None:
        boundaries = (
            ("character", self.character, CharacterPrompt),
            ("rag", self.rag, RagContext),
            ("history", self.history, MaskedHistory),
            ("current_user", self.current_user, CurrentUserMessage),
            ("budget", self.budget, TokenBudget),
        )
        for field, value, expected_type in boundaries:
            if not isinstance(value, expected_type):
                raise TypeError(f"{field} must be a {expected_type.__name__}")


@dataclass(frozen=True, repr=False)
class PromptMessage:
    role: PromptRole
    content: str


@dataclass(frozen=True)
class PromptUsage:
    total: int
    character: int
    rag: int
    history: int
    current_user: int
    post_history: int
    omitted_rag_items: int
    omitted_history_exchanges: int


@dataclass(frozen=True, repr=False)
class BuiltPrompt:
    messages: tuple[PromptMessage, ...]
    usage: PromptUsage


class PromptInputLimitError(ValueError):
    def __init__(self, region: str, used: int, limit: int) -> None:
        self.region = region
        self.used = used
        self.limit = limit
        super().__init__(
            f"required prompt region exceeds token budget: "
            f"region={region} used={used} limit={limit}"
        )

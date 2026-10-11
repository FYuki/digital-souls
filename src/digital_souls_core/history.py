"""Storage-independent conversation contracts and trusted authorization boundary."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol

from pydantic import Field, model_validator

from .character import AccessScope
from .contracts import InputMessage, Message, Name, NamedToolChoice, StrictModel, Tool

type Clock = Callable[[], datetime]


def current_utc() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("turn clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


class TurnInput(StrictModel):
    request_id: Name
    expected_revision: Annotated[int, Field(ge=0)]
    messages: Annotated[list[InputMessage], Field(min_length=1)]
    memory_excluded_indices: list[Annotated[int, Field(ge=0)]] = Field(default_factory=list)
    stream: bool = False
    tools: Annotated[list[Tool], Field(min_length=1)] | None = None
    tool_choice: Literal["auto", "none", "required"] | NamedToolChoice | None = None
    temperature: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] | None = None
    max_tokens: Annotated[int, Field(gt=0, le=32768)] | None = None
    max_completion_tokens: Annotated[int, Field(gt=0, le=32768)] | None = None

    @model_validator(mode="after")
    def valid_exclusions(self) -> "TurnInput":
        if len(set(self.memory_excluded_indices)) != len(self.memory_excluded_indices) or any(
            index >= len(self.messages) for index in self.memory_excluded_indices
        ):
            raise ValueError("invalid message exclusion indices")
        return self


class ConversationControls(StrictModel):
    expected_revision: Annotated[int, Field(ge=0)]
    private_mode: bool | None = None
    archived: bool | None = None

    @model_validator(mode="after")
    def has_change(self) -> "ConversationControls":
        if self.private_mode is None and self.archived is None:
            raise ValueError("at least one control is required")
        return self


class MemoryConfirmation(StrictModel):
    expected_revision: Annotated[int, Field(ge=0)]
    turn_revision: Annotated[int, Field(gt=0)]
    message_index: Annotated[int, Field(ge=0)]
    accept_private_mode: bool


class TurnDeletionInput(StrictModel):
    expected_revision: Annotated[int, Field(ge=0)]
    turn_revision: Annotated[int, Field(gt=0)]
    scope: Literal["selected", "following"]


@dataclass(frozen=True)
class TurnDeletionResult:
    conversation_id: str
    revision: int
    turn_revisions: tuple[int, ...]


@dataclass(frozen=True)
class TurnDeletion:
    """Content-free notification identifying exactly the removed turns."""

    event_id: str
    conversation_id: str
    turn_revisions: tuple[int, ...]


@dataclass(frozen=True)
class SourceReference:
    """Immutable history address within a separately supplied trusted Binding."""

    conversation_id: str
    turn_revision: int
    message_index: int


@dataclass(frozen=True)
class SourceState:
    """Per-message eligibility metadata; not an approval to create a memory."""

    reference: SourceReference
    eligible: bool
    stated_at: datetime | None


@dataclass(frozen=True)
class SourceDeletion:
    """Content-free durable notification; all sources through the revision are gone."""

    event_id: str
    conversation_id: str
    through_revision: int


@dataclass(frozen=True)
class Binding:
    scope: AccessScope
    character_id: str


@dataclass(frozen=True)
class TurnMetadata:
    """Identity of the Card used for the saved assistant response in this turn."""

    turn_revision: int
    card_sha256: str | None


@dataclass(frozen=True)
class Snapshot:
    conversation_id: str
    revision: int
    messages: tuple[Message, ...]
    private_mode: bool = False
    archived: bool = False
    memory_sources: tuple[SourceState, ...] = ()
    memory_confirmations: tuple[SourceReference, ...] = ()
    turns: tuple[TurnMetadata, ...] = ()


@dataclass(frozen=True)
class Receipt:
    fingerprint: str
    revision: int
    message: Message
    finish_reason: str
    memory_confirmation_indices: tuple[int, ...] = ()
    card_sha256: str | None = None


type Operation = Literal["create", "list", "read", "export", "store"]


class HistoryPolicy(Protocol):
    """Operator-injected stage-2 seam; absent/uncertain policy must deny.

    Checks include secrets in tool arguments/results as well as visible text.
    HTTP callers cannot install this policy or assert that input is safe.
    Deletion is authorized by binding alone so revoked consent cannot trap data.
    """

    def allows(
        self, operation: Operation, binding: Binding, messages: tuple[Message, ...]
    ) -> bool: ...


class HistoryStore(Protocol):
    def create(self, binding: Binding) -> Snapshot: ...
    def list(self, binding: Binding, *, include_archived: bool = False) -> list[str]: ...
    def read(self, binding: Binding, conversation_id: str) -> Snapshot: ...
    def receipt(
        self, binding: Binding, conversation_id: str, request_id: str
    ) -> Receipt | None: ...
    def append(
        self,
        binding: Binding,
        conversation_id: str,
        request_id: str,
        fingerprint: str,
        expected_revision: int,
        messages: tuple[Message, ...],
        finish_reason: str,
        *,
        memory_excluded_indices: tuple[int, ...] = (),
        memory_confirmation_indices: tuple[int, ...] = (),
        card_sha256: str | None = None,
    ) -> Receipt: ...
    def delete(self, binding: Binding, conversation_id: str) -> None: ...

    def delete_turns(
        self, binding: Binding, conversation_id: str, selection: TurnDeletionInput
    ) -> TurnDeletionResult: ...

    def turn_deletions(self, binding: Binding) -> tuple[TurnDeletion, ...]: ...

    def acknowledge_turn_deletion(self, binding: Binding, event_id: str) -> None: ...

    def controls(
        self, binding: Binding, conversation_id: str, changes: ConversationControls
    ) -> Snapshot: ...

    def source_eligible(self, binding: Binding, source: SourceReference) -> bool: ...

    def confirm(
        self, binding: Binding, conversation_id: str, answer: MemoryConfirmation
    ) -> Snapshot: ...

    def deletions(self, binding: Binding) -> tuple[SourceDeletion, ...]: ...

    def acknowledge_deletion(self, binding: Binding, event_id: str) -> None: ...

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from app.conversation_history.models import TurnStatus

MAX_FREE_TEXT_LENGTH = 60


class MemoryType(str, Enum):
    EPISODIC_EVENT = "EPISODIC_EVENT"
    USER_PREFERENCE = "USER_PREFERENCE"
    INTERACTION_PREFERENCE = "INTERACTION_PREFERENCE"


class EpisodicEventType(str, Enum):
    SHARED_MILESTONE = "SHARED_MILESTONE"
    ACHIEVEMENT = "ACHIEVEMENT"
    DECISION = "DECISION"
    OUTCOME = "OUTCOME"
    CHANGE = "CHANGE"


class EpisodicSubject(str, Enum):
    USER = "USER"
    SHARED = "SHARED"


class PreferencePolarity(str, Enum):
    LIKE = "LIKE"
    DISLIKE = "DISLIKE"
    PREFER_OVER = "PREFER_OVER"


class InteractionAspect(str, Enum):
    ADDRESSING = "ADDRESSING"
    TONE = "TONE"
    RESPONSE_FORMAT = "RESPONSE_FORMAT"
    RESPONSE_LENGTH = "RESPONSE_LENGTH"
    LANGUAGE = "LANGUAGE"


class RagAdmissionDecision(str, Enum):
    DENY_SENSITIVE = "DENY_SENSITIVE"
    DENY_USER_REQUEST = "DENY_USER_REQUEST"
    ABSTAIN_UNKNOWN = "ABSTAIN_UNKNOWN"
    NOT_MEMORY_WORTHY = "NOT_MEMORY_WORTHY"
    ALLOW_STRUCTURED = "ALLOW_STRUCTURED"


def _validate_free_text(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_FREE_TEXT_LENGTH
    ):
        raise ValueError(
            f"{field_name} must contain 1 to {MAX_FREE_TEXT_LENGTH} characters"
        )


@dataclass(frozen=True)
class EpisodicEventValue:
    event_type: EpisodicEventType
    subject: EpisodicSubject
    topic: str

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, EpisodicEventType):
            raise TypeError("event_type must be an EpisodicEventType")
        if not isinstance(self.subject, EpisodicSubject):
            raise TypeError("subject must be an EpisodicSubject")
        _validate_free_text(self.topic, "topic")


@dataclass(frozen=True)
class UserPreferenceValue:
    polarity: PreferencePolarity
    object: str
    alternative: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.polarity, PreferencePolarity):
            raise TypeError("polarity must be a PreferencePolarity")
        _validate_free_text(self.object, "object")
        if self.polarity is PreferencePolarity.PREFER_OVER:
            if self.alternative is None:
                raise ValueError("PREFER_OVER requires alternative")
            _validate_free_text(self.alternative, "alternative")
        elif self.alternative is not None:
            raise ValueError("alternative is only valid for PREFER_OVER")


@dataclass(frozen=True)
class InteractionPreferenceValue:
    aspect: InteractionAspect
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.aspect, InteractionAspect):
            raise TypeError("aspect must be an InteractionAspect")
        _validate_free_text(self.value, "value")


StructuredValue: TypeAlias = (
    EpisodicEventValue | UserPreferenceValue | InteractionPreferenceValue
)


@dataclass(frozen=True)
class ConversationSource:
    turn_status: TurnStatus
    history_content_stored: bool

    def __post_init__(self) -> None:
        if not isinstance(self.turn_status, TurnStatus):
            raise TypeError("turn_status must be a TurnStatus")
        if not isinstance(self.history_content_stored, bool):
            raise TypeError("history_content_stored must be a bool")


@dataclass(frozen=True)
class MemoryCandidate:
    memory_type: MemoryType
    structured_value: StructuredValue
    source: ConversationSource | None

    def __post_init__(self) -> None:
        expected_value_types = {
            MemoryType.EPISODIC_EVENT: EpisodicEventValue,
            MemoryType.USER_PREFERENCE: UserPreferenceValue,
            MemoryType.INTERACTION_PREFERENCE: InteractionPreferenceValue,
        }
        if not isinstance(self.memory_type, MemoryType):
            raise TypeError("memory_type must be a MemoryType")
        if not isinstance(
            self.structured_value,
            expected_value_types[self.memory_type],
        ):
            raise ValueError("structured_value does not match memory_type")
        if self.source is not None and not isinstance(self.source, ConversationSource):
            raise TypeError("source must be a ConversationSource or None")


@dataclass(frozen=True)
class ApprovedMemoryCandidate:
    structured_value: StructuredValue
    normalized_text: str

    def __post_init__(self) -> None:
        if not isinstance(
            self.structured_value,
            (EpisodicEventValue, UserPreferenceValue, InteractionPreferenceValue),
        ):
            raise TypeError("structured_value must be an allowlist value")
        if (
            not isinstance(self.normalized_text, str)
            or not self.normalized_text.strip()
        ):
            raise ValueError("normalized_text must not be blank")


@dataclass(frozen=True)
class RagAdmissionResult:
    decision: RagAdmissionDecision
    candidate: ApprovedMemoryCandidate | None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, RagAdmissionDecision):
            raise TypeError("decision must be a RagAdmissionDecision")
        if self.decision is RagAdmissionDecision.ALLOW_STRUCTURED:
            if not isinstance(self.candidate, ApprovedMemoryCandidate):
                raise ValueError("ALLOW_STRUCTURED requires an approved candidate")
        elif self.candidate is not None:
            raise ValueError("non-allow decisions must not contain a candidate")

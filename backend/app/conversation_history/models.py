from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID


class TurnStatus(str, Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    PRIVACY_SKIPPED = "privacy_skipped"


class PrivacySkipReason(str, Enum):
    SENSITIVE_CONTENT = "sensitive_content"
    POLICY_DENIED = "policy_denied"


@dataclass(frozen=True)
class Conversation:
    character_id: str
    conversation_id: UUID
    created_at: datetime


@dataclass(frozen=True)
class ConversationTurn:
    turn_id: UUID
    character_id: str
    conversation_id: UUID
    user_content: str | None
    assistant_content: str | None
    status: TurnStatus
    privacy_reason_code: PrivacySkipReason | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ProcessingTurnInput:
    sanitized_user_content: str


@dataclass(frozen=True)
class PrivacySkippedTurnInput:
    reason_code: PrivacySkipReason

    def __post_init__(self) -> None:
        if not isinstance(self.reason_code, PrivacySkipReason):
            raise TypeError("reason_code must be a PrivacySkipReason")

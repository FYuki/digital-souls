from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from app.privacy.contracts import HistoryDecisionReasonCode
from app.conversation_history.titles import DEFAULT_CONVERSATION_TITLE


class TurnStatus(str, Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    PRIVACY_SKIPPED = "privacy_skipped"


@dataclass(frozen=True)
class Conversation:
    character_id: str
    conversation_id: UUID
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    title: str = DEFAULT_CONVERSATION_TITLE
    title_is_manual: bool = False


@dataclass(frozen=True)
class ConversationTurn:
    turn_id: UUID
    character_id: str
    conversation_id: UUID
    user_content: str | None
    assistant_content: str | None
    status: TurnStatus
    privacy_reason_code: HistoryDecisionReasonCode | None
    created_at: datetime
    updated_at: datetime
    sanitizer_version: str | None = None
    policy_version: str | None = None


@dataclass(frozen=True)
class ProcessingTurnInput:
    sanitized_user_content: str


@dataclass(frozen=True)
class PrivacySkippedTurnInput:
    reason_code: HistoryDecisionReasonCode
    sanitizer_version: str
    policy_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason_code, HistoryDecisionReasonCode):
            raise TypeError("reason_code must be a HistoryDecisionReasonCode")
        if not self.sanitizer_version.strip():
            raise ValueError("sanitizer_version must not be empty")
        if not self.policy_version.strip():
            raise ValueError("policy_version must not be empty")

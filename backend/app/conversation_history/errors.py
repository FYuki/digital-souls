from uuid import UUID
from typing import Literal

from app.conversation_history.models import TurnStatus


class ConversationHistoryError(Exception):
    """会話履歴操作で呼び出し側が識別する基底例外。"""


class LegacySchemaError(ConversationHistoryError):
    """現行schema以外のDBが指定された。"""


class SchemaRollbackError(RuntimeError):
    """schema migrationと補償処理がともに失敗した。"""

    def __init__(
        self,
        primary_error: Exception,
        compensation_error: Exception,
        compensation_stage: Literal["restore", "verification"],
    ) -> None:
        super().__init__("schema migration rollback failed")
        self.primary_error = primary_error
        self.compensation_error = compensation_error
        self.compensation_stage = compensation_stage


class InvalidConversationIdError(ConversationHistoryError):
    def __init__(self, conversation_id: UUID) -> None:
        super().__init__("conversation_id must be UUIDv4")
        self.conversation_id = conversation_id


class InvalidUtcDatetimeError(ConversationHistoryError):
    def __init__(self) -> None:
        super().__init__("clock must return a timezone-aware datetime")


class ConversationNotFoundError(ConversationHistoryError):
    def __init__(self) -> None:
        super().__init__("conversation was not found")


class ConversationCharacterBoundaryError(ConversationNotFoundError):
    pass


class TurnNotFoundError(ConversationHistoryError):
    def __init__(self) -> None:
        super().__init__("turn was not found")


class InvalidStateTransitionError(ConversationHistoryError):
    def __init__(
        self,
        current_status: TurnStatus,
        requested_status: TurnStatus,
    ) -> None:
        super().__init__(
            f"turn cannot transition from {current_status.value} "
            f"to {requested_status.value}"
        )
        self.current_status = current_status
        self.requested_status = requested_status

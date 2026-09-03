from typing import Literal
from uuid import UUID

from pydantic import BaseModel, field_validator

from app.chat_service import (
    PersistedContentTurn,
    PersistedPrivacySkippedTurn,
    PersistedTurn,
)
from app.conversation_history.models import Conversation, ConversationTurn, TurnStatus
from app.conversation_history.titles import normalize_manual_conversation_title
from app.privacy.contracts import HistoryDecisionReasonCode


class ConversationResponse(BaseModel):
    character_id: str
    conversation_id: UUID
    created_at: str
    updated_at: str
    archived_at: str | None
    title: str


class RenameConversationRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return normalize_manual_conversation_title(value)


class ContentTurnResponse(BaseModel):
    kind: Literal["content"] = "content"
    turn_id: UUID
    user_content: str
    assistant_content: str


class PrivacySkippedTurnResponse(BaseModel):
    kind: Literal["privacy_skipped"] = "privacy_skipped"
    turn_id: UUID
    reason_code: HistoryDecisionReasonCode
    sanitizer_version: str
    policy_version: str


TurnResponse = ContentTurnResponse | PrivacySkippedTurnResponse


def conversation_response(conversation: Conversation) -> ConversationResponse:
    return ConversationResponse(
        character_id=conversation.character_id,
        conversation_id=conversation.conversation_id,
        created_at=conversation.created_at.isoformat(),
        updated_at=conversation.updated_at.isoformat(),
        archived_at=(
            None
            if conversation.archived_at is None
            else conversation.archived_at.isoformat()
        ),
        title=conversation.title,
    )


def history_turn_response(turn: ConversationTurn) -> TurnResponse:
    if turn.status is TurnStatus.PRIVACY_SKIPPED:
        if (
            turn.privacy_reason_code is None
            or turn.sanitizer_version is None
            or turn.policy_version is None
        ):
            raise ValueError("privacy_skipped turn requires metadata")
        return PrivacySkippedTurnResponse(
            turn_id=turn.turn_id,
            reason_code=turn.privacy_reason_code,
            sanitizer_version=turn.sanitizer_version,
            policy_version=turn.policy_version,
        )
    if turn.status not in {TurnStatus.COMPLETED, TurnStatus.INTERRUPTED}:
        raise ValueError("only persisted turn states can be returned")
    if turn.user_content is None or turn.assistant_content is None:
        raise ValueError("content turn requires persisted user and assistant content")
    return ContentTurnResponse(
        turn_id=turn.turn_id,
        user_content=turn.user_content,
        assistant_content=turn.assistant_content,
    )


def persisted_turn_response(turn: PersistedTurn) -> TurnResponse:
    if isinstance(turn, PersistedContentTurn):
        return ContentTurnResponse(
            turn_id=turn.turn_id,
            user_content=turn.user_content,
            assistant_content=turn.assistant_content,
        )
    if isinstance(turn, PersistedPrivacySkippedTurn):
        return PrivacySkippedTurnResponse(
            turn_id=turn.turn_id,
            reason_code=turn.reason_code,
            sanitizer_version=turn.sanitizer_version,
            policy_version=turn.policy_version,
        )
    raise TypeError("unsupported persisted turn")

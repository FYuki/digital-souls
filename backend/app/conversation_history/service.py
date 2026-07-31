from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.conversation_history.models import (
    PrivacySkipReason,
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
    TurnStatus,
)
from app.conversation_history.repository import ConversationHistoryRepository
from app.privacy.contracts import (
    ConversationHistoryAction,
    ConversationHistoryDecision,
    HistoryDecisionReasonCode,
)
from app.privacy.history_sanitizer import HistorySanitizer


@dataclass(frozen=True)
class StartedHistoryTurn:
    turn_id: UUID
    content_skipped: bool


@dataclass(frozen=True, repr=False)
class CompletedHistoryExchange:
    user_content: str
    assistant_content: str


class HistorySession(Protocol):
    def start_turn(self, user_content: str) -> StartedHistoryTurn:
        ...

    def complete_turn(
        self,
        started_turn: StartedHistoryTurn,
        assistant_content: str,
    ) -> None:
        ...

    def fail_turn(self, started_turn: StartedHistoryTurn) -> None:
        ...

    def completed_exchanges(self) -> tuple[CompletedHistoryExchange, ...]:
        ...


class HistoryService(Protocol):
    def open_session(self, character_id: str) -> HistorySession:
        ...


class ConversationHistorySession:
    def __init__(
        self,
        character_id: str,
        conversation_id: UUID,
        repository: ConversationHistoryRepository,
        sanitizer: HistorySanitizer,
    ) -> None:
        self._character_id = character_id
        self._conversation_id = conversation_id
        self._repository = repository
        self._sanitizer = sanitizer

    def start_turn(self, user_content: str) -> StartedHistoryTurn:
        decision = self._sanitizer.sanitize_current_user(user_content)
        if decision.action is ConversationHistoryAction.SKIP_CONTENT:
            turn = self._repository.create_privacy_skipped_turn(
                self._character_id,
                self._conversation_id,
                PrivacySkippedTurnInput(reason_code=_privacy_skip_reason(decision)),
            )
            return StartedHistoryTurn(turn.turn_id, content_skipped=True)
        if decision.content is None:
            raise ValueError("STORE_MASKED decision requires content")
        turn = self._repository.create_processing_turn(
            self._character_id,
            self._conversation_id,
            ProcessingTurnInput(sanitized_user_content=decision.content),
        )
        return StartedHistoryTurn(turn.turn_id, content_skipped=False)

    def complete_turn(
        self,
        started_turn: StartedHistoryTurn,
        assistant_content: str,
    ) -> None:
        if started_turn.content_skipped:
            return
        decision = self._sanitizer.sanitize_assistant(assistant_content)
        if decision.action is ConversationHistoryAction.SKIP_CONTENT:
            self._repository.skip_processing_turn_for_privacy(
                self._character_id,
                self._conversation_id,
                started_turn.turn_id,
                PrivacySkippedTurnInput(reason_code=_privacy_skip_reason(decision)),
            )
            return
        if decision.content is None:
            raise ValueError("STORE_MASKED decision requires content")
        self._repository.complete_turn(
            self._character_id,
            self._conversation_id,
            started_turn.turn_id,
            sanitized_assistant_content=decision.content,
        )

    def fail_turn(self, started_turn: StartedHistoryTurn) -> None:
        if started_turn.content_skipped:
            return
        self._repository.fail_turn(
            self._character_id,
            self._conversation_id,
            started_turn.turn_id,
        )

    def completed_exchanges(self) -> tuple[CompletedHistoryExchange, ...]:
        turns = self._repository.list_turns(
            self._character_id,
            self._conversation_id,
        )
        return tuple(
            CompletedHistoryExchange(
                user_content=turn.user_content,
                assistant_content=turn.assistant_content,
            )
            for turn in turns
            if turn.status is TurnStatus.COMPLETED
            and turn.user_content is not None
            and turn.assistant_content is not None
        )


class ConversationHistoryService:
    def __init__(
        self,
        repository: ConversationHistoryRepository,
        sanitizer: HistorySanitizer,
    ) -> None:
        self._repository = repository
        self._sanitizer = sanitizer

    def open_session(self, character_id: str) -> ConversationHistorySession:
        conversation = self._repository.create_conversation(character_id)
        return ConversationHistorySession(
            character_id,
            conversation.conversation_id,
            self._repository,
            self._sanitizer,
        )


def _privacy_skip_reason(
    decision: ConversationHistoryDecision,
) -> PrivacySkipReason:
    if decision.reason_code is HistoryDecisionReasonCode.STORAGE_OPT_OUT:
        return PrivacySkipReason.POLICY_DENIED
    return PrivacySkipReason.SENSITIVE_CONTENT

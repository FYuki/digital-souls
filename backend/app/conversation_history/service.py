from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.conversation_history.models import (
    ConversationTurn,
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
)
from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.prompt_history import (
    RestoredHistoryTurn,
    restore_prompt_turn,
)
from app.privacy.contracts import (
    ConversationHistoryAction,
    ConversationHistoryDecision,
)
from app.privacy.history_sanitizer import HistorySanitizer
from app.voice_session.playback_range import played_text_prefix


@dataclass(frozen=True)
class StartedHistoryTurn:
    turn_id: UUID
    content_skipped: bool
    initial_turn: ConversationTurn | None = None


class HistorySession(Protocol):
    def start_turn(self, user_content: str) -> StartedHistoryTurn:
        ...

    def complete_turn(
        self,
        started_turn: StartedHistoryTurn,
        assistant_content: str,
    ) -> ConversationTurn:
        ...

    def interrupt_turn(
        self,
        started_turn: StartedHistoryTurn,
        generated_text: str,
        response_audio_chunks: Sequence[Mapping[str, object]],
        last_played_sequence: int,
    ) -> ConversationTurn:
        ...

    def fail_turn(self, started_turn: StartedHistoryTurn) -> None:
        ...

    def prompt_turns(
        self,
        *,
        max_completed_turns: int,
        page_size: int,
    ) -> Iterator[RestoredHistoryTurn]:
        ...


class HistoryService(Protocol):
    def open_session(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> HistorySession:
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
                _privacy_skip_input(decision),
            )
            return StartedHistoryTurn(
                turn.turn_id,
                content_skipped=True,
                initial_turn=turn,
            )
        if decision.content is None:
            raise ValueError("STORE_MASKED decision requires content")
        turn = self._repository.create_processing_turn(
            self._character_id,
            self._conversation_id,
            ProcessingTurnInput(sanitized_user_content=decision.content),
        )
        return StartedHistoryTurn(
            turn.turn_id,
            content_skipped=False,
            initial_turn=turn,
        )

    def complete_turn(
        self,
        started_turn: StartedHistoryTurn,
        assistant_content: str,
    ) -> ConversationTurn:
        decision = self._sanitizer.sanitize_assistant(assistant_content)
        if started_turn.content_skipped:
            if started_turn.initial_turn is None:
                raise ValueError("privacy-skipped started turn requires persisted turn")
            return started_turn.initial_turn
        if decision.action is ConversationHistoryAction.SKIP_CONTENT:
            return self._repository.skip_processing_turn_for_privacy(
                self._character_id,
                self._conversation_id,
                started_turn.turn_id,
                _privacy_skip_input(decision),
            )
        if decision.content is None:
            raise ValueError("STORE_MASKED decision requires content")
        return self._repository.complete_turn(
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

    def interrupt_turn(
        self,
        started_turn: StartedHistoryTurn,
        generated_text: str,
        response_audio_chunks: Sequence[Mapping[str, object]],
        last_played_sequence: int,
    ) -> ConversationTurn:
        played_content = played_text_prefix(
            generated_text,
            response_audio_chunks,
            last_played_sequence=last_played_sequence,
        )
        decision = self._sanitizer.sanitize_assistant(played_content)
        if started_turn.content_skipped:
            if started_turn.initial_turn is None:
                raise ValueError("privacy-skipped started turn requires persisted turn")
            return started_turn.initial_turn
        if decision.action is ConversationHistoryAction.SKIP_CONTENT:
            return self._repository.skip_processing_turn_for_privacy(
                self._character_id,
                self._conversation_id,
                started_turn.turn_id,
                _privacy_skip_input(decision),
            )
        if decision.content is None:
            raise ValueError("STORE_MASKED decision requires content")
        return self._repository.interrupt_turn(
            self._character_id,
            self._conversation_id,
            started_turn.turn_id,
            sanitized_assistant_content=decision.content,
        )

    def prompt_turns(
        self,
        *,
        max_completed_turns: int,
        page_size: int,
    ) -> Iterator[RestoredHistoryTurn]:
        if max_completed_turns < 1:
            raise ValueError("max_completed_turns must be positive")
        completed = 0
        cursor = None
        while completed < max_completed_turns:
            page = self._repository.list_prompt_turns_page(
                self._character_id,
                self._conversation_id,
                cursor=cursor,
                page_size=page_size,
            )
            for turn in page.turns:
                restored_turn = restore_prompt_turn(turn)
                yield restored_turn
                if restored_turn.is_completed:
                    completed += 1
                    if completed == max_completed_turns:
                        return
            cursor = page.next_cursor
            if cursor is None:
                return


class ConversationHistoryService:
    def __init__(
        self,
        repository: ConversationHistoryRepository,
        sanitizer: HistorySanitizer,
    ) -> None:
        self._repository = repository
        self._sanitizer = sanitizer

    def open_session(
        self,
        character_id: str,
        conversation_id: UUID,
    ) -> ConversationHistorySession:
        conversation = self._repository.resume_conversation(
            character_id,
            conversation_id,
        )
        return ConversationHistorySession(
            character_id,
            conversation.conversation_id,
            self._repository,
            self._sanitizer,
        )


def _privacy_skip_input(
    decision: ConversationHistoryDecision,
) -> PrivacySkippedTurnInput:
    return PrivacySkippedTurnInput(
        reason_code=decision.reason_code,
        sanitizer_version=decision.sanitizer_version,
        policy_version=decision.policy_version,
    )

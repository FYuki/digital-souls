import inspect
from pathlib import Path

import pytest

from app.conversation_history.errors import (
    InvalidStateTransitionError,
    TurnNotFoundError,
)
from app.conversation_history.models import (
    PrivacySkipReason,
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
    TurnStatus,
)
from app.conversation_history.turn_state import require_turn_transition
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    OTHER_CONVERSATION_ID,
    TURN_ID,
    SequenceUuidFactory,
    create_repository,
)


def _processing_turn(database_path: Path):
    repository = create_repository(database_path)
    repository.create_conversation("miori")
    repository.create_processing_turn(
        "miori",
        CONVERSATION_ID,
        ProcessingTurnInput(sanitized_user_content="処理済みの質問"),
    )
    return repository


class TestTurnTransitions:
    @pytest.mark.parametrize(
        ("current", "requested"),
        [
            (current, requested)
            for current in TurnStatus
            for requested in TurnStatus
        ],
    )
    def test_turn_state_rule_covers_every_status_pair(
        self,
        current: TurnStatus,
        requested: TurnStatus,
    ) -> None:
        allowed = {
            (TurnStatus.PROCESSING, TurnStatus.COMPLETED),
            (TurnStatus.PROCESSING, TurnStatus.FAILED),
            (TurnStatus.COMPLETED, TurnStatus.FAILED),
        }

        if (current, requested) in allowed:
            require_turn_transition(current, requested)
            return

        with pytest.raises(InvalidStateTransitionError) as captured:
            require_turn_transition(current, requested)

        assert captured.value.current_status is current
        assert captured.value.requested_status is requested

    def test_should_complete_processing_turn_with_full_assistant_body(
        self,
        tmp_path: Path,
    ) -> None:
        repository = _processing_turn(tmp_path / "history.db")

        completed = repository.complete_turn(
            "miori",
            CONVERSATION_ID,
            TURN_ID,
            sanitized_assistant_content="完全な回答です。",
        )

        assert completed.status is TurnStatus.COMPLETED
        assert completed.assistant_content == "完全な回答です。"

    def test_should_fail_processing_turn_before_assistant_body(
        self,
        tmp_path: Path,
    ) -> None:
        repository = _processing_turn(tmp_path / "history.db")

        failed = repository.fail_turn("miori", CONVERSATION_ID, TURN_ID)

        assert failed.status is TurnStatus.FAILED
        assert failed.assistant_content is None

    def test_should_preserve_assistant_body_when_completed_turn_fails(
        self,
        tmp_path: Path,
    ) -> None:
        repository = _processing_turn(tmp_path / "history.db")
        repository.complete_turn(
            "miori",
            CONVERSATION_ID,
            TURN_ID,
            sanitized_assistant_content="送信済みの完全回答",
        )

        failed = repository.fail_turn("miori", CONVERSATION_ID, TURN_ID)

        assert failed.status is TurnStatus.FAILED
        assert failed.assistant_content == "送信済みの完全回答"

    @pytest.mark.parametrize("terminal_status", ["failed", "privacy_skipped"])
    def test_should_reject_transition_from_terminal_status(
        self,
        tmp_path: Path,
        terminal_status: str,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(database_path)
        repository.create_conversation("miori")
        if terminal_status == "failed":
            repository.create_processing_turn(
                "miori",
                CONVERSATION_ID,
                ProcessingTurnInput(sanitized_user_content="処理済み本文"),
            )
            repository.fail_turn("miori", CONVERSATION_ID, TURN_ID)
        else:
            repository.create_privacy_skipped_turn(
                "miori",
                CONVERSATION_ID,
                PrivacySkippedTurnInput(
                    reason_code=PrivacySkipReason.POLICY_DENIED,
                ),
            )

        with pytest.raises(InvalidStateTransitionError) as captured:
            repository.complete_turn(
                "miori",
                CONVERSATION_ID,
                TURN_ID,
                sanitized_assistant_content="保存してはいけない本文",
            )

        assert captured.value.current_status.value == terminal_status
        assert captured.value.requested_status is TurnStatus.COMPLETED
        assert "保存してはいけない本文" not in str(captured.value)

    def test_should_reject_completed_to_completed_transition(
        self,
        tmp_path: Path,
    ) -> None:
        repository = _processing_turn(tmp_path / "history.db")
        repository.complete_turn(
            "miori",
            CONVERSATION_ID,
            TURN_ID,
            sanitized_assistant_content="最初の完全回答",
        )

        with pytest.raises(InvalidStateTransitionError):
            repository.complete_turn(
                "miori",
                CONVERSATION_ID,
                TURN_ID,
                sanitized_assistant_content="上書き回答",
            )

    def test_should_not_update_turn_through_other_character_boundary(
        self,
        tmp_path: Path,
    ) -> None:
        repository = _processing_turn(tmp_path / "history.db")

        with pytest.raises(TurnNotFoundError):
            repository.fail_turn("akira", CONVERSATION_ID, TURN_ID)

        remaining = repository.list_turns("miori", CONVERSATION_ID)
        assert remaining[0].status is TurnStatus.PROCESSING

    def test_should_not_update_turn_through_other_conversation_boundary(
        self,
        tmp_path: Path,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = create_repository(
            database_path,
            uuid_factory=SequenceUuidFactory(
                CONVERSATION_ID,
                TURN_ID,
                OTHER_CONVERSATION_ID,
            ),
        )
        repository.create_conversation("miori")
        repository.create_processing_turn(
            "miori",
            CONVERSATION_ID,
            ProcessingTurnInput(sanitized_user_content="処理済みの質問"),
        )
        other = repository.create_conversation("miori")
        assert other.conversation_id == OTHER_CONVERSATION_ID

        with pytest.raises(TurnNotFoundError):
            repository.fail_turn("miori", OTHER_CONVERSATION_ID, TURN_ID)

        remaining = repository.list_turns("miori", CONVERSATION_ID)
        assert remaining[0].status is TurnStatus.PROCESSING

    def test_should_not_expose_streaming_fragment_operation(self) -> None:
        from app.conversation_history.repository import ConversationHistoryRepository

        public_methods = {
            name
            for name, member in inspect.getmembers(
                ConversationHistoryRepository,
                predicate=inspect.isfunction,
            )
            if not name.startswith("_")
        }

        assert public_methods == {
            "create_conversation",
            "resume_conversation",
            "create_processing_turn",
            "create_privacy_skipped_turn",
            "complete_turn",
            "fail_turn",
            "recover_stale_processing",
            "list_turns",
        }

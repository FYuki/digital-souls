import inspect
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest

from app.conversation_history.errors import (
    ConversationNotFoundError,
    InvalidStateTransitionError,
    TurnNotFoundError,
)
from app.conversation_history.models import (
    PrivacySkippedTurnInput,
    ProcessingTurnInput,
    TurnStatus,
)
from app.privacy.contracts import HistoryDecisionReasonCode
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
    def test_get_turn_returns_the_authoritative_turn_or_none(
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

        assert repository.get_turn("miori", CONVERSATION_ID, TURN_ID) == completed
        assert repository.get_turn("akira", CONVERSATION_ID, TURN_ID) is None
        assert repository.get_turn(
            "miori",
            CONVERSATION_ID,
            UUID("30000000-0000-4000-8000-000000000001"),
        ) is None

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
            ("processing", "completed"),
            ("processing", "failed"),
            ("processing", "privacy_skipped"),
            ("processing", "interrupted"),
            ("completed", "failed"),
        }

        if (current.value, requested.value) in allowed:
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

    @pytest.mark.parametrize("heard_content", ["再生済みの範囲", ""])
    def test_should_interrupt_processing_turn_with_only_heard_content(
        self,
        tmp_path: Path,
        heard_content: str,
    ) -> None:
        repository = _processing_turn(tmp_path / "history.db")

        interrupted = repository.interrupt_turn(
            "miori",
            CONVERSATION_ID,
            TURN_ID,
            sanitized_assistant_content=heard_content,
        )

        assert interrupted.status is TurnStatus.INTERRUPTED
        assert interrupted.assistant_content == heard_content

    def test_should_reject_interrupting_completed_turn_without_overwrite(
        self,
        tmp_path: Path,
    ) -> None:
        repository = _processing_turn(tmp_path / "history.db")
        repository.complete_turn(
            "miori",
            CONVERSATION_ID,
            TURN_ID,
            sanitized_assistant_content="完了時に保存した回答",
        )

        with pytest.raises(InvalidStateTransitionError):
            repository.interrupt_turn(
                "miori",
                CONVERSATION_ID,
                TURN_ID,
                sanitized_assistant_content="上書きしてはいけない回答",
            )

        stored = repository.get_turn("miori", CONVERSATION_ID, TURN_ID)
        assert stored is not None
        assert stored.status is TurnStatus.COMPLETED
        assert stored.assistant_content == "完了時に保存した回答"

    def test_should_clear_processing_content_when_privacy_sanitization_fails(
        self,
        tmp_path: Path,
    ) -> None:
        repository = _processing_turn(tmp_path / "history.db")

        skipped = repository.skip_processing_turn_for_privacy(
            "miori",
            CONVERSATION_ID,
            TURN_ID,
            PrivacySkippedTurnInput(
                reason_code=HistoryDecisionReasonCode.SCAN_FAILURE,
                sanitizer_version="test-sanitizer-v1",
                policy_version="test-policy-v1",
            ),
        )

        assert skipped.status is TurnStatus.PRIVACY_SKIPPED
        assert skipped.user_content is None
        assert skipped.assistant_content is None
        assert skipped.privacy_reason_code is HistoryDecisionReasonCode.SCAN_FAILURE

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

    @pytest.mark.parametrize(
        "operation",
        ["complete", "fail", "privacy_skip", "interrupt"],
    )
    def test_should_reject_every_turn_transition_after_archive(
        self,
        tmp_path: Path,
        operation: str,
    ) -> None:
        database_path = tmp_path / "history.db"
        repository = _processing_turn(database_path)
        repository.archive_conversation("miori", CONVERSATION_ID)

        with pytest.raises(ConversationNotFoundError):
            if operation == "complete":
                repository.complete_turn(
                    "miori",
                    CONVERSATION_ID,
                    TURN_ID,
                    sanitized_assistant_content="保存してはいけない回答",
                )
            elif operation == "fail":
                repository.fail_turn("miori", CONVERSATION_ID, TURN_ID)
            elif operation == "privacy_skip":
                repository.skip_processing_turn_for_privacy(
                    "miori",
                    CONVERSATION_ID,
                    TURN_ID,
                    PrivacySkippedTurnInput(
                        reason_code=HistoryDecisionReasonCode.SCAN_FAILURE,
                        sanitizer_version="test-sanitizer-v1",
                        policy_version="test-policy-v1",
                    ),
                )
            elif operation == "interrupt":
                repository.interrupt_turn(
                    "miori",
                    CONVERSATION_ID,
                    TURN_ID,
                    sanitized_assistant_content="保存してはいけない回答",
                )
            else:
                raise AssertionError(f"未対応の操作です: {operation}")

        with sqlite3.connect(database_path) as connection:
            stored = connection.execute(
                "SELECT status, assistant_content FROM conversation_turns "
                "WHERE turn_id = ?",
                (str(TURN_ID),),
            ).fetchone()
        assert stored == (TurnStatus.PROCESSING.value, None)

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
                    reason_code=HistoryDecisionReasonCode.STORAGE_OPT_OUT,
                    sanitizer_version="test-sanitizer-v1",
                    policy_version="test-policy-v1",
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

        with pytest.raises(ConversationNotFoundError):
            repository.fail_turn("akira", CONVERSATION_ID, TURN_ID)

        with sqlite3.connect(tmp_path / "history.db") as connection:
            remaining_status = connection.execute(
                "SELECT status FROM conversation_turns WHERE turn_id = ?",
                (str(TURN_ID),),
            ).fetchone()[0]
        assert remaining_status == TurnStatus.PROCESSING.value

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

        with sqlite3.connect(database_path) as connection:
            remaining_status = connection.execute(
                "SELECT status FROM conversation_turns WHERE turn_id = ?",
                (str(TURN_ID),),
            ).fetchone()[0]
        assert remaining_status == TurnStatus.PROCESSING.value

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
            "archive_conversation",
            "complete_turn",
            "fail_turn",
            "get_turn",
            "get_previous_completed_turn",
            "hard_delete_conversation",
            "interrupt_turn",
            "list_active_conversations",
            "list_archived_conversations",
            "skip_processing_turn_for_privacy",
            "recover_stale_processing",
            "unarchive_conversation",
            "list_turns",
            "list_history_turns",
            "list_prompt_turns_page",
            "consolidation_activity",
        }

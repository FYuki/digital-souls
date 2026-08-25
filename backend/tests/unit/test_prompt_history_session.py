from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

from app.conversation_history.models import ConversationTurn, TurnStatus
from app.conversation_history.service import ConversationHistorySession
from tests.conversation_history_test_support import CONVERSATION_ID


TURN_IDS = tuple(
    UUID(f"9e70795d-e5d5-431d-baa2-67f8844030{index:02d}")
    for index in range(40, 48)
)


def _turn(
    index: int,
    status: TurnStatus,
    *,
    assistant_content: str | None = None,
) -> ConversationTurn:
    timestamp = datetime(2026, 7, 24, 12, index, tzinfo=UTC)
    if status is TurnStatus.COMPLETED and assistant_content is None:
        assistant_content = f"assistant-{index}"
    if status.value == "interrupted" and assistant_content is None:
        assistant_content = f"heard-assistant-{index}"
    return ConversationTurn(
        turn_id=TURN_IDS[index],
        character_id="miori",
        conversation_id=CONVERSATION_ID,
        user_content=f"user-{index}",
        assistant_content=assistant_content,
        status=status,
        privacy_reason_code=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _page(turns, cursor):
    return SimpleNamespace(turns=tuple(turns), next_cursor=cursor)


def _session(repository: MagicMock) -> ConversationHistorySession:
    return ConversationHistorySession(
        character_id="miori",
        conversation_id=CONVERSATION_ID,
        repository=repository,
        sanitizer=MagicMock(),
    )


class TestPromptHistorySession:
    def test_should_restore_interrupted_partial_without_counting_it_as_completed(
        self,
    ) -> None:
        repository = MagicMock()
        repository.list_prompt_turns_page.side_effect = [
            _page((_turn(3, TurnStatus.INTERRUPTED),), "cursor-1"),
            _page((_turn(2, TurnStatus.COMPLETED),), None),
        ]

        restored = tuple(
            _session(repository).prompt_turns(
                max_completed_turns=1,
                page_size=1,
            )
        )

        assert [turn.assistant_content for turn in restored] == [
            "heard-assistant-3",
            "assistant-2",
        ]
        assert [turn.is_completed for turn in restored] == [False, True]
        assert repository.list_prompt_turns_page.call_count == 2

    def test_should_count_only_completed_and_stop_at_boundary(self) -> None:
        repository = MagicMock()
        repository.list_prompt_turns_page.side_effect = [
            _page(
                (
                    _turn(5, TurnStatus.FAILED),
                    _turn(4, TurnStatus.COMPLETED),
                    _turn(3, TurnStatus.FAILED),
                ),
                "cursor-1",
            ),
            _page(
                (
                    _turn(2, TurnStatus.COMPLETED),
                    _turn(1, TurnStatus.FAILED),
                ),
                "cursor-2",
            ),
        ]

        restored = tuple(
            _session(repository).prompt_turns(
                max_completed_turns=2,
                page_size=3,
            )
        )

        assert [turn.user_content for turn in restored] == [
            "user-5",
            "user-4",
            "user-3",
            "user-2",
        ]
        assert [turn.is_completed for turn in restored] == [False, True, False, True]
        assert repository.list_prompt_turns_page.call_count == 2

    def test_should_scan_until_cursor_is_exhausted_when_completed_is_short(
        self,
    ) -> None:
        repository = MagicMock()
        repository.list_prompt_turns_page.side_effect = [
            _page((_turn(3, TurnStatus.FAILED),), "cursor-1"),
            _page((_turn(2, TurnStatus.COMPLETED),), "cursor-2"),
            _page((_turn(1, TurnStatus.FAILED),), None),
        ]

        restored = tuple(
            _session(repository).prompt_turns(
                max_completed_turns=2,
                page_size=1,
            )
        )

        assert [turn.user_content for turn in restored] == [
            "user-3",
            "user-2",
            "user-1",
        ]
        assert repository.list_prompt_turns_page.call_count == 3

    def test_should_preserve_only_saved_failed_assistant_content(self) -> None:
        repository = MagicMock()
        repository.list_prompt_turns_page.return_value = _page(
            (
                _turn(3, TurnStatus.FAILED, assistant_content="saved assistant"),
                _turn(2, TurnStatus.FAILED, assistant_content=""),
                _turn(1, TurnStatus.FAILED, assistant_content=None),
            ),
            None,
        )

        restored = tuple(
            _session(repository).prompt_turns(
                max_completed_turns=1,
                page_size=3,
            )
        )

        assert [turn.assistant_content for turn in restored] == [
            "saved assistant",
            None,
            None,
        ]

    def test_should_use_prompt_page_api_instead_of_full_list(self) -> None:
        repository = MagicMock()
        repository.list_prompt_turns_page.return_value = _page((), None)

        tuple(
            _session(repository).prompt_turns(
                max_completed_turns=10,
                page_size=32,
            )
        )

        repository.list_prompt_turns_page.assert_called_once_with(
            "miori",
            CONVERSATION_ID,
            cursor=None,
            page_size=32,
        )
        repository.list_turns.assert_not_called()

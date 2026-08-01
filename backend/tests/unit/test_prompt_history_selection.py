import importlib
from dataclasses import dataclass
from typing import cast

import pytest

from app.prompting import PromptMessage, PromptRole


def _history_types():
    prompting = importlib.import_module("app.prompting")
    return (
        getattr(prompting, "MaskedHistoryTurn"),
        getattr(prompting, "MaskedHistory"),
    )


@dataclass
class RecordingMessageCounter:
    counts: dict[str, int]

    def __post_init__(self) -> None:
        self.calls: list[tuple[PromptMessage, ...]] = []

    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        self.calls.append(messages)
        return sum(self.counts.get(message.content, 1) for message in messages)


def _turn(user: str, assistant: str | None, *, completed: bool):
    turn_type, _ = _history_types()
    return turn_type(
        user_content=user,
        assistant_content=assistant,
        is_completed=completed,
    )


def _select(turns, counter: RecordingMessageCounter, limit: int):
    module = importlib.import_module("app.prompting.history")
    return module.select_history(
        turns,
        token_counter=counter,
        token_limit=limit,
    )


class TestHistoryTokenSelection:
    @pytest.mark.parametrize(
        ("completed_index", "limit", "expected_users"),
        [
            (0, 100, tuple(f"turn-{index}" for index in reversed(range(40)))),
            (35, 1, ("turn-35",)),
        ],
    )
    def test_should_select_streaming_history_with_immutable_state_transitions(
        self,
        completed_index: int,
        limit: int,
        expected_users: tuple[str, ...],
    ) -> None:
        counter = RecordingMessageCounter({})
        newest_first = tuple(
            _turn(
                f"turn-{index}",
                None,
                completed=index == completed_index,
            )
            for index in range(40)
        )

        selected = _select(newest_first, counter, limit)

        assert tuple(turn.user_content for turn in selected.turns) == expected_users
        assert selected.omitted_turns == 40 - len(expected_users)

    def test_should_measure_candidates_in_final_oldest_first_order(self) -> None:
        counter = RecordingMessageCounter({})
        newest_first = (
            _turn("new-user", "new-assistant", completed=True),
            _turn("old-failed", None, completed=False),
        )

        _select(newest_first, counter, limit=10)

        assert [message.content for message in counter.calls[-1]] == [
            "old-failed",
            "new-user",
            "new-assistant",
        ]

    def test_should_not_restore_older_failed_after_newer_failed_overflows(self) -> None:
        counter = RecordingMessageCounter({"new-failed": 2, "old-failed": 1})
        newest_first = (
            _turn("new-failed", None, completed=False),
            _turn("old-failed", None, completed=False),
        )

        selected = _select(newest_first, counter, limit=1)

        assert selected.turns == ()
        assert selected.omitted_turns == 2

    def test_should_remove_only_oldest_optional_turn_for_latest_completed(
        self,
    ) -> None:
        counter = RecordingMessageCounter(
            {
                "new-failed": 1,
                "old-failed": 3,
                "completed-user": 1,
                "completed-assistant": 1,
            }
        )
        newest_first = (
            _turn("new-failed", None, completed=False),
            _turn("old-failed", None, completed=False),
            _turn("completed-user", "completed-assistant", completed=True),
        )

        selected = _select(newest_first, counter, limit=5)

        assert [turn.user_content for turn in selected.turns] == [
            "completed-user",
            "new-failed",
        ]
        assert selected.omitted_turns == 1

    def test_should_not_restore_older_failed_when_newer_failed_does_not_fit(
        self,
    ) -> None:
        counter = RecordingMessageCounter(
            {
                "new-failed": 2,
                "completed": 1,
                "old-failed": 1,
            }
        )
        newest_first = (
            _turn("new-failed", None, completed=False),
            _turn("completed", None, completed=True),
            _turn("old-failed", None, completed=False),
        )

        selected = _select(newest_first, counter, limit=2)

        assert [turn.user_content for turn in selected.turns] == ["completed"]
        assert selected.omitted_turns == 2

    def test_should_not_restore_older_failed_in_streaming_selection(self) -> None:
        counter = RecordingMessageCounter(
            {
                "new-failed": 2,
                "completed": 1,
                "old-failed": 1,
                **{f"overflow-{index}": 10 for index in range(30)},
            }
        )
        newest_first = (
            _turn("new-failed", None, completed=False),
            _turn("completed", None, completed=True),
            _turn("old-failed", None, completed=False),
            *(
                _turn(f"overflow-{index}", None, completed=False)
                for index in range(30)
            ),
        )

        selected = _select(newest_first, counter, limit=2)

        assert [turn.user_content for turn in selected.turns] == ["completed"]
        assert selected.omitted_turns == 32

    def test_should_close_older_streaming_selection_after_first_overflow(
        self,
    ) -> None:
        counter = RecordingMessageCounter(
            {
                "completed": 1,
                "overflowing-failed": 2,
                **{f"older-{index}": 1 for index in range(31)},
            }
        )
        newest_first = (
            _turn("completed", None, completed=True),
            _turn("overflowing-failed", None, completed=False),
            *(
                _turn(f"older-{index}", None, completed=False)
                for index in range(31)
            ),
        )

        selected = _select(newest_first, counter, limit=1)

        assert [turn.user_content for turn in selected.turns] == ["completed"]
        assert selected.omitted_turns == 32
        assert len(counter.calls) <= 5

    def test_should_not_restore_older_optional_turn_after_selection_closes(
        self,
    ) -> None:
        counter = RecordingMessageCounter(
            {
                "new-failed": 1,
                "overflowing-failed": 5,
                "older-failed": 1,
                "completed-user": 1,
                "completed-assistant": 1,
            }
        )
        newest_first = (
            _turn("new-failed", None, completed=False),
            _turn("overflowing-failed", None, completed=False),
            _turn("older-failed", None, completed=False),
            _turn("completed-user", "completed-assistant", completed=True),
        )

        selected = _select(newest_first, counter, limit=3)

        assert [turn.user_content for turn in selected.turns] == [
            "completed-user",
            "new-failed",
        ]
        assert selected.omitted_turns == 2

    def test_should_consume_closed_iterator_without_recounting_older_turns(
        self,
    ) -> None:
        counter = RecordingMessageCounter(
            {
                "completed": 1,
                "overflowing-failed": 2,
                "older-failed": 1,
            }
        )
        consumed: list[str] = []

        def newest_first():
            for turn in (
                _turn("completed", None, completed=True),
                _turn("overflowing-failed", None, completed=False),
                _turn("older-failed", None, completed=False),
            ):
                consumed.append(turn.user_content)
                yield turn

        selected = _select(newest_first(), counter, limit=1)

        assert consumed == ["completed", "overflowing-failed", "older-failed"]
        assert [turn.user_content for turn in selected.turns] == ["completed"]
        assert selected.omitted_turns == 2
        assert len(counter.calls) == 2

    def test_should_remove_oldest_turn_without_splitting_completed_pair(self) -> None:
        counter = RecordingMessageCounter(
            {"new-user": 2, "new-assistant": 2, "old-user": 2, "old-assistant": 2}
        )
        newest_first = (
            _turn("new-user", "new-assistant", completed=True),
            _turn("old-user", "old-assistant", completed=True),
        )

        selected = _select(newest_first, counter, limit=4)

        assert [turn.user_content for turn in selected.turns] == ["new-user"]
        assert selected.turns[0].assistant_content == "new-assistant"
        assert selected.omitted_turns == 1

    @pytest.mark.parametrize(
        ("limit", "expected_turns", "expected_omitted"),
        [
            (5, (("completed-user", "completed-assistant"),), 1),
            (
                6,
                (
                    ("failed-user", "failed-assistant"),
                    ("completed-user", "completed-assistant"),
                ),
                0,
            ),
        ],
    )
    def test_should_keep_or_remove_failed_turn_without_splitting_saved_messages(
        self,
        limit: int,
        expected_turns: tuple[tuple[str, str], ...],
        expected_omitted: int,
    ) -> None:
        counter = RecordingMessageCounter(
            {
                "completed-user": 1,
                "completed-assistant": 1,
                "failed-user": 2,
                "failed-assistant": 2,
            }
        )
        newest_first = (
            _turn("completed-user", "completed-assistant", completed=True),
            _turn("failed-user", "failed-assistant", completed=False),
        )

        selected = _select(newest_first, counter, limit=limit)

        assert tuple(
            (turn.user_content, turn.assistant_content) for turn in selected.turns
        ) == expected_turns
        assert selected.omitted_turns == expected_omitted

    def test_should_keep_latest_completed_even_when_newer_failed_exists(self) -> None:
        counter = RecordingMessageCounter(
            {"failed": 3, "completed-user": 3, "completed-assistant": 3}
        )
        newest_first = (
            _turn("failed", None, completed=False),
            _turn("completed-user", "completed-assistant", completed=True),
        )

        selected = _select(newest_first, counter, limit=1)

        assert [turn.user_content for turn in selected.turns] == ["completed-user"]
        assert selected.turns[0].is_completed is True

    def test_should_not_apply_minimum_retention_when_there_is_no_completed_turn(
        self,
    ) -> None:
        counter = RecordingMessageCounter({"failed": 2})

        selected = _select(
            (_turn("failed", None, completed=False),),
            counter,
            limit=1,
        )

        assert selected.turns == ()
        assert selected.omitted_turns == 1

    def test_should_measure_the_actual_role_content_message_representation(self) -> None:
        counter = RecordingMessageCounter({"failed-user": 1, "saved-assistant": 1})

        _select(
            (_turn("failed-user", "saved-assistant", completed=False),),
            counter,
            limit=10,
        )

        measured = [message for call in counter.calls for message in call]
        assert PromptMessage(PromptRole.USER, "failed-user") in measured
        assert PromptMessage(PromptRole.ASSISTANT, "saved-assistant") in measured

    def test_should_hide_saved_bodies_from_turn_and_history_repr(self) -> None:
        _, history_type = _history_types()
        turn = _turn("MASKED_USER_SECRET", "MASKED_ASSISTANT_SECRET", completed=True)
        history = history_type(turns=(turn,), omitted_turns=0)

        representation = repr((turn, history))

        assert "MASKED_USER_SECRET" not in representation
        assert "MASKED_ASSISTANT_SECRET" not in representation

    def test_should_reject_current_user_type_at_saved_history_boundary(self) -> None:
        prompting = importlib.import_module("app.prompting")
        _, history_type = _history_types()

        with pytest.raises(TypeError, match="MaskedHistoryTurn"):
            history_type(
                turns=(prompting.CurrentUserMessage("raw-current-user"),),
                omitted_turns=0,
            )


def test_should_restore_failed_messages_without_inventing_assistant_content() -> None:
    module = importlib.import_module("app.conversation_history.prompt_history")
    restore = getattr(module, "restore_prompt_turn")
    turn = cast(object, None)
    models = importlib.import_module("app.conversation_history.models")
    from datetime import UTC, datetime
    from uuid import UUID

    turn = models.ConversationTurn(
        turn_id=UUID("9e70795d-e5d5-431d-baa2-67f884403020"),
        character_id="miori",
        conversation_id=UUID("e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010"),
        user_content="MASKED_FAILED_USER",
        assistant_content=None,
        status=models.TurnStatus.FAILED,
        privacy_reason_code=None,
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        updated_at=datetime(2026, 7, 24, tzinfo=UTC),
    )

    restored = restore(turn)

    assert restored.user_content == "MASKED_FAILED_USER"
    assert restored.assistant_content is None
    assert restored.is_completed is False


def test_should_preserve_saved_empty_assistant_content_for_completed_turn() -> None:
    module = importlib.import_module("app.conversation_history.prompt_history")
    restore = getattr(module, "restore_prompt_turn")
    models = importlib.import_module("app.conversation_history.models")
    from datetime import UTC, datetime
    from uuid import UUID

    turn = models.ConversationTurn(
        turn_id=UUID("9e70795d-e5d5-431d-baa2-67f884403021"),
        character_id="miori",
        conversation_id=UUID("e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010"),
        user_content="MASKED_COMPLETED_USER",
        assistant_content="",
        status=models.TurnStatus.COMPLETED,
        privacy_reason_code=None,
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        updated_at=datetime(2026, 7, 24, tzinfo=UTC),
    )

    restored = restore(turn)

    assert restored.assistant_content == ""
    assert restored.is_completed is True

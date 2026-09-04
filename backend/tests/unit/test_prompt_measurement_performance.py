from collections import Counter
from collections.abc import Iterator

import pytest

from app import chat_prompt
from app.conversation_history.prompt_history import RestoredHistoryTurn
from app.prompting import (
    BuiltPrompt,
    CharacterPrompt,
    CurrentUserMessage,
    MaskedHistory,
    MaskedHistoryTurn,
    PromptBuilder,
    PromptMessage,
    PromptRole,
    RagContext,
    RagItem,
)
from app.model_settings import resolve_model_settings
from app.prompting.history import select_history
from app.prompting.measurement import TokenMeasurements
from tests.prompt_test_support import prompt_build_input, token_budget


class RecordingMessageCounter:
    def __init__(self) -> None:
        self.calls: list[tuple[PromptMessage, ...]] = []

    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        self.calls.append(messages)
        return len(messages)

    @property
    def resent_messages(self) -> int:
        return sum(len(messages) for messages in self.calls)

    @property
    def duplicate_calls(self) -> int:
        frequencies = Counter(self.calls)
        return sum(count - 1 for count in frequencies.values())


class RecordingHistorySession:
    def __init__(self, turns: tuple[RestoredHistoryTurn, ...]) -> None:
        self._turns = turns
        self.prompt_turns_calls: list[tuple[int, int]] = []

    def prompt_turns(
        self,
        *,
        max_completed_turns: int,
        page_size: int,
    ) -> Iterator[RestoredHistoryTurn]:
        self.prompt_turns_calls.append((max_completed_turns, page_size))
        return iter(self._turns)


def test_should_return_new_measurement_state_without_changing_previous_state() -> None:
    counter = RecordingMessageCounter()
    initial = TokenMeasurements(counter)
    messages = (PromptMessage(PromptRole.USER, "immutable-state"),)

    first = initial.measure(messages)
    repeated = first.measurements.measure(messages)
    measured_again_from_initial = initial.measure(messages)

    assert first.count == 1
    assert repeated.count == 1
    assert measured_again_from_initial.count == 1
    assert repeated.measurements is first.measurements
    assert len(counter.calls) == 2


def _completed_turns_newest_first(count: int) -> tuple[RestoredHistoryTurn, ...]:
    return tuple(
        RestoredHistoryTurn(
            user_content=f"history-user-{index}",
            assistant_content=f"history-assistant-{index}",
            is_completed=True,
        )
        for index in range(count, 0, -1)
    )


def _build_chat_prompt(
    *,
    history_count: int,
    rag_count: int,
) -> tuple[BuiltPrompt, RecordingMessageCounter, RecordingHistorySession]:
    counter = RecordingMessageCounter()
    history_session = RecordingHistorySession(
        _completed_turns_newest_first(history_count)
    )
    result = chat_prompt.build_chat_prompt(
        character=CharacterPrompt(
            description="character-description",
            personality="character-personality",
            scenario="character-scenario",
            system_prompt="character-system",
            mes_example="character-example",
            post_history_instructions="post-history",
        ),
        rag=RagContext(
            items=tuple(
                RagItem(f"rag-{index}", raw_distance=float(index))
                for index in range(1, rag_count + 1)
            )
        ),
        current_user=CurrentUserMessage("current-user"),
        history_session=history_session,
        config=resolve_model_settings(
            {
                "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": str(history_count),
                "CONVERSATION_HISTORY_TOKEN_LIMIT": "1000",
                "USER_INPUT_TOKEN_LIMIT": "1000",
            },
            chat_context_tokens=2000,
            assistant_max_generation_tokens=1,
        ),
        token_counter=counter,
    )
    return result, counter, history_session


@pytest.mark.parametrize(
    ("history_count", "rag_count", "expected_calls", "expected_resent_messages"),
    [(10, 5, 6, 56), (20, 10, 6, 106)],
)
def test_should_measure_untrimmed_prompt_without_quadratic_resends(
    history_count: int,
    rag_count: int,
    expected_calls: int,
    expected_resent_messages: int,
) -> None:
    _, counter, _ = _build_chat_prompt(
        history_count=history_count,
        rag_count=rag_count,
    )

    actual = (len(counter.calls), counter.resent_messages, counter.duplicate_calls)
    assert actual == (expected_calls, expected_resent_messages, 0)


def test_should_preserve_prompt_order_and_usage_after_reusing_measurements() -> None:
    result, _, history_session = _build_chat_prompt(
        history_count=10,
        rag_count=5,
    )

    assert history_session.prompt_turns_calls == [(10, 32)]
    assert [message.role for message in result.messages] == [
        PromptRole.SYSTEM,
        *(PromptRole.SYSTEM for _ in range(5)),
        *(
            role
            for _ in range(10)
            for role in (PromptRole.USER, PromptRole.ASSISTANT)
        ),
        PromptRole.SYSTEM,
        PromptRole.USER,
    ]
    assert [message.content for message in result.messages[6:10]] == [
        "history-user-1",
        "history-assistant-1",
        "history-user-2",
        "history-assistant-2",
    ]
    assert result.messages[-1] == PromptMessage(PromptRole.USER, "current-user")
    assert sum(message.content == "current-user" for message in result.messages) == 1
    assert result.usage.total == 28
    assert result.usage.character == 1
    assert result.usage.rag == 5
    assert result.usage.history == 20
    assert result.usage.current_user == 1
    assert result.usage.post_history == 1


def test_should_find_history_limit_without_measuring_every_cumulative_prefix() -> None:
    counter = RecordingMessageCounter()
    newest_first = tuple(
        MaskedHistoryTurn(
            user_content=f"saved-user-{index}",
            assistant_content=f"saved-assistant-{index}",
            is_completed=index % 2 == 0,
        )
        for index in range(10, 0, -1)
    )

    selected = select_history(
        newest_first,
        token_counter=counter,
        token_limit=18,
    )

    assert [turn.user_content for turn in selected.turns] == [
        f"saved-user-{index}" for index in range(2, 11)
    ]
    assert all(turn.assistant_content is not None for turn in selected.turns)
    assert selected.omitted_turns == 1
    actual_measurement = (
        len(counter.calls),
        counter.resent_messages,
        counter.duplicate_calls,
    )
    assert (
        actual_measurement[0] <= 6
        and actual_measurement[1] <= 80
        and actual_measurement[2] == 0
    ), actual_measurement


@pytest.mark.parametrize(
    "completed_indices",
    [frozenset(range(1, 41)), frozenset({35}), frozenset()],
)
def test_should_measure_more_than_one_history_page_without_staged_prefixes(
    completed_indices: frozenset[int],
) -> None:
    counter = RecordingMessageCounter()
    newest_first = tuple(
        MaskedHistoryTurn(
            user_content=f"saved-user-{index}",
            assistant_content=f"saved-assistant-{index}",
            is_completed=index in completed_indices,
        )
        for index in range(40, 0, -1)
    )

    selected = select_history(
        newest_first,
        token_counter=counter,
        token_limit=1_000,
    )

    assert len(selected.turns) == 40
    assert selected.omitted_turns == 0
    history_call_sizes = [len(messages) for messages in counter.calls]
    assert history_call_sizes == [64, 80]
    assert counter.resent_messages == 144
    assert counter.duplicate_calls == 0


def test_should_stream_many_failed_turns_without_materializing_or_measuring_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yielded = 0
    yielded_at_measurement: list[int] = []
    counter = RecordingMessageCounter()

    class FailedHistorySession:
        def prompt_turns(
            self,
            *,
            max_completed_turns: int,
            page_size: int,
        ) -> Iterator[RestoredHistoryTurn]:
            del max_completed_turns, page_size

            def turns() -> Iterator[RestoredHistoryTurn]:
                nonlocal yielded
                for index in range(10_000):
                    yielded += 1
                    yield RestoredHistoryTurn(
                        user_content=f"failed-{index}",
                        assistant_content=None,
                        is_completed=False,
                    )

            return turns()

    def count(messages: tuple[PromptMessage, ...]) -> int:
        yielded_at_measurement.append(yielded)
        return counter.count_input_tokens(messages)

    class YieldAwareCounter:
        def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
            return count(messages)

    result = chat_prompt.build_chat_prompt(
        character=CharacterPrompt("", "", "", "system", "", ""),
        rag=RagContext(items=()),
        current_user=CurrentUserMessage("current-user"),
        history_session=FailedHistorySession(),
        config=resolve_model_settings(
            {
                "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": "10",
                "CONVERSATION_HISTORY_TOKEN_LIMIT": "1",
                "USER_INPUT_TOKEN_LIMIT": "10",
            },
            chat_context_tokens=20,
            assistant_max_generation_tokens=1,
        ),
        token_counter=YieldAwareCounter(),
    )

    history_calls = [
        (index, messages)
        for index, messages in enumerate(counter.calls)
        if any(message.content.startswith("failed-") for message in messages)
    ]
    assert yielded == 10_000
    assert yielded_at_measurement[history_calls[0][0]] == 33
    assert max(len(messages) for _, messages in history_calls) <= 32
    assert result.usage.omitted_history_exchanges == 9_999
    assert [message.content for message in result.messages][-2:] == [
        "failed-0",
        "current-user",
    ]


def test_should_find_rag_limit_without_measuring_every_cumulative_prefix() -> None:
    counter = RecordingMessageCounter()
    prompt_input = prompt_build_input(
        rag=RagContext(
            items=tuple(
                RagItem(f"rag-{index}", raw_distance=float(index))
                for index in range(8)
            )
        ),
        history=MaskedHistory(turns=(), omitted_turns=0),
        budget=token_budget(total=100, rag=3),
    )

    result = PromptBuilder(counter).build(prompt_input)

    selected_rag = [
        message.content
        for message in result.messages
        if "## 関連する記憶" in message.content
    ]
    assert selected_rag == [
        "## 関連する記憶\nrag-0",
        "## 関連する記憶\nrag-1",
        "## 関連する記憶\nrag-2",
    ]
    assert result.usage.omitted_rag_items == 5
    actual_measurement = (
        len(counter.calls),
        counter.resent_messages,
        counter.duplicate_calls,
    )
    assert (
        actual_measurement[0] <= 9 and actual_measurement[2] == 0
    ), actual_measurement


def test_should_fit_total_limit_without_remeasuring_prompt_after_each_removal() -> None:
    counter = RecordingMessageCounter()
    history = MaskedHistory(
        turns=tuple(
            MaskedHistoryTurn(
                user_content=f"history-user-{index}",
                assistant_content=f"history-assistant-{index}",
                is_completed=True,
            )
            for index in range(6)
        ),
        omitted_turns=0,
    )
    prompt_input = prompt_build_input(
        rag=RagContext(
            items=tuple(
                RagItem(f"rag-{index}", raw_distance=float(index))
                for index in range(6)
            )
        ),
        history=history,
        budget=token_budget(total=10, rag=20, history=20),
    )

    result = PromptBuilder(counter).build(prompt_input)

    assert result.usage.omitted_rag_items == 6
    assert result.usage.omitted_history_exchanges == 3
    assert [message.content for message in result.messages[1:7]] == [
        "history-user-3",
        "history-assistant-3",
        "history-user-4",
        "history-assistant-4",
        "history-user-5",
        "history-assistant-5",
    ]
    assert result.usage.total == 9
    actual_measurement = (
        len(counter.calls),
        counter.resent_messages,
        counter.duplicate_calls,
    )
    assert (
        actual_measurement[0] <= 18 and actual_measurement[2] == 0
    ), actual_measurement


def test_should_scope_measurement_reuse_to_each_build_request() -> None:
    counter = RecordingMessageCounter()
    builder = PromptBuilder(counter)
    first_input = prompt_build_input(current_user=CurrentUserMessage("request-one"))
    second_input = prompt_build_input(current_user=CurrentUserMessage("request-two"))

    first_result = builder.build(first_input)
    first_request_calls = tuple(counter.calls)
    second_result = builder.build(second_input)
    second_request_calls = tuple(counter.calls[len(first_request_calls) :])

    assert (len(first_request_calls), len(second_request_calls)) == (6, 6)
    assert first_result.messages[-1].content == "request-one"
    assert second_result.messages[-1].content == "request-two"
    assert any(
        any(message.content == "request-two" for message in call)
        for call in second_request_calls
    )
    assert all(
        all(message.content != "request-one" for message in call)
        for call in second_request_calls
    )

from collections.abc import Iterable
from typing import Protocol

from app.prompting.models import (
    MaskedHistory,
    MaskedHistoryTurn,
    PromptMessage,
    PromptRole,
)


class MessageTokenCounter(Protocol):
    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        ...


class SavedHistoryTurn(Protocol):
    @property
    def user_content(self) -> str:
        ...

    @property
    def assistant_content(self) -> str | None:
        ...

    @property
    def is_completed(self) -> bool:
        ...


def turn_messages(turn: MaskedHistoryTurn) -> tuple[PromptMessage, ...]:
    messages = [PromptMessage(PromptRole.USER, turn.user_content)]
    if turn.assistant_content is not None:
        messages.append(
            PromptMessage(PromptRole.ASSISTANT, turn.assistant_content)
        )
    return tuple(messages)


def select_history(
    newest_first: Iterable[SavedHistoryTurn],
    *,
    token_counter: MessageTokenCounter,
    token_limit: int,
) -> MaskedHistory:
    if token_limit < 0:
        raise ValueError("token_limit must be non-negative")
    selected: list[MaskedHistoryTurn] = []
    omitted = 0
    latest_completed_selected = False
    older_selection_closed = False
    for source_turn in newest_first:
        turn = MaskedHistoryTurn(
            source_turn.user_content,
            source_turn.assistant_content,
            source_turn.is_completed,
        )
        if turn.is_completed and not latest_completed_selected:
            selected.append(turn)
            latest_completed_selected = True
            selected_tokens = _count_turns(tuple(selected), token_counter)
            while len(selected) > 1 and selected_tokens > token_limit:
                selected.pop(-2)
                omitted += 1
                selected_tokens = _count_turns(tuple(selected), token_counter)
            continue
        if older_selection_closed:
            omitted += 1
            continue
        candidate = (*selected, turn)
        if _count_turns(candidate, token_counter) > token_limit:
            omitted += 1
            older_selection_closed = True
            continue
        selected.append(turn)
    return MaskedHistory(turns=tuple(reversed(selected)), omitted_turns=omitted)


def _count_turns(
    turns: tuple[MaskedHistoryTurn, ...],
    token_counter: MessageTokenCounter,
) -> int:
    messages = tuple(
        message
        for turn in reversed(turns)
        for message in turn_messages(turn)
    )
    return token_counter.count_input_tokens(messages)

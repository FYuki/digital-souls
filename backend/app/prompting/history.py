from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from itertools import chain, islice
from typing import Protocol

from app.prompting.measurement import TokenCounter, TokenMeasurement, TokenMeasurements
from app.prompting.models import (
    MaskedHistory,
    MaskedHistoryTurn,
    PromptMessage,
    PromptRole,
)

_BUFFERED_HISTORY_TURN_LIMIT = 32


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


@dataclass(frozen=True)
class HistorySelection:
    history: MaskedHistory
    measurements: TokenMeasurements


def turn_messages(turn: MaskedHistoryTurn) -> tuple[PromptMessage, ...]:
    assistant = (
        ()
        if turn.assistant_content is None
        else (PromptMessage(PromptRole.ASSISTANT, turn.assistant_content),)
    )
    return (PromptMessage(PromptRole.USER, turn.user_content), *assistant)


def select_history(
    newest_first: Iterable[SavedHistoryTurn],
    *,
    token_counter: TokenCounter,
    token_limit: int,
) -> MaskedHistory:
    selection = select_history_with_measurements(
        newest_first,
        measurements=TokenMeasurements(token_counter),
        token_limit=token_limit,
    )
    return selection.history


def select_history_with_measurements(
    newest_first: Iterable[SavedHistoryTurn],
    *,
    measurements: TokenMeasurements,
    token_limit: int,
) -> HistorySelection:
    if token_limit < 0:
        raise ValueError("token_limit must be non-negative")
    source = iter(newest_first)
    buffered = tuple(islice(source, _BUFFERED_HISTORY_TURN_LIMIT + 1))
    if len(buffered) <= _BUFFERED_HISTORY_TURN_LIMIT:
        return _select_buffered_history(buffered, measurements, token_limit)
    return _select_streaming_history(
        chain(buffered, source), measurements, token_limit
    )


@dataclass(frozen=True)
class _StreamingSelection:
    measurements: TokenMeasurements
    selected: tuple[MaskedHistoryTurn, ...] = ()
    pending: tuple[MaskedHistoryTurn, ...] = ()
    omitted: int = 0
    batch_size: int = _BUFFERED_HISTORY_TURN_LIMIT
    selection_closed: bool = False
    latest_completed_found: bool = False


def _select_streaming_history(
    newest_first: Iterable[SavedHistoryTurn],
    measurements: TokenMeasurements,
    token_limit: int,
) -> HistorySelection:
    state = _StreamingSelection(measurements)
    for source_turn in newest_first:
        state = _consume_streaming_turn(state, _masked_turn(source_turn), token_limit)
    if state.pending:
        state = _flush_streaming_pending(state, token_limit)
    return HistorySelection(
        MaskedHistory(
            turns=tuple(reversed(state.selected)),
            omitted_turns=state.omitted,
        ),
        state.measurements,
    )


def _consume_streaming_turn(
    state: _StreamingSelection,
    turn: MaskedHistoryTurn,
    token_limit: int,
) -> _StreamingSelection:
    if state.selection_closed:
        if turn.is_completed and not state.latest_completed_found:
            return _retain_streaming_latest_completed(state, turn, token_limit)
        return replace(state, omitted=state.omitted + 1)
    if turn.is_completed and not state.latest_completed_found and not state.selected:
        queued = replace(
            state,
            pending=(*state.pending, turn),
            latest_completed_found=True,
        )
        if len(queued.pending) < queued.batch_size:
            return queued
        flushed = _flush_streaming_pending(queued, token_limit)
        if flushed.selection_closed:
            return flushed
        return replace(flushed, batch_size=flushed.batch_size * 2)
    if turn.is_completed and not state.latest_completed_found:
        flushed = _flush_streaming_pending(state, token_limit)
        return _retain_streaming_latest_completed(flushed, turn, token_limit)
    queued = replace(state, pending=(*state.pending, turn))
    if len(queued.pending) < queued.batch_size:
        return queued
    flushed = _flush_streaming_pending(queued, token_limit)
    if flushed.selection_closed:
        return flushed
    return replace(flushed, batch_size=flushed.batch_size * 2)


def _retain_streaming_latest_completed(
    state: _StreamingSelection,
    turn: MaskedHistoryTurn,
    token_limit: int,
) -> _StreamingSelection:
    selected, removed, closed, measurements = _retain_latest_completed(
        state.selected, turn, state.measurements, token_limit
    )
    return replace(
        state,
        measurements=measurements,
        selected=selected,
        omitted=state.omitted + removed,
        selection_closed=state.selection_closed or closed,
        latest_completed_found=True,
    )


def _flush_streaming_pending(
    state: _StreamingSelection,
    token_limit: int,
) -> _StreamingSelection:
    if (
        not state.selected
        and state.pending
        and state.latest_completed_found
    ):
        complete = _count_turns(state.pending, state.measurements)
        if complete.count <= token_limit:
            return replace(
                state,
                measurements=complete.measurements,
                selected=state.pending,
                pending=(),
            )
        selected, measurements = _select_priority_turns(
            state.pending,
            complete.measurements,
            token_limit,
        )
        omitted = len(state.pending) - len(selected)
        return replace(
            state,
            measurements=measurements,
            selected=selected,
            pending=(),
            omitted=state.omitted + omitted,
            selection_closed=omitted > 0,
        )
    selected, omitted, closed, measurements = _flush_pending(
        state.selected,
        state.pending,
        state.measurements,
        token_limit,
    )
    return replace(
        state,
        measurements=measurements,
        selected=selected,
        pending=(),
        omitted=state.omitted + omitted,
        selection_closed=closed,
    )


def _select_buffered_history(
    source_turns: tuple[SavedHistoryTurn, ...],
    measurements: TokenMeasurements,
    token_limit: int,
) -> HistorySelection:
    turns = tuple(_masked_turn(turn) for turn in source_turns)
    if not turns:
        return HistorySelection(MaskedHistory(turns=(), omitted_turns=0), measurements)
    measured = _count_turns(turns, measurements)
    if measured.count <= token_limit:
        return HistorySelection(
            MaskedHistory(turns=tuple(reversed(turns)), omitted_turns=0),
            measured.measurements,
        )
    selected, measurements = _select_priority_turns(
        turns, measured.measurements, token_limit
    )
    return HistorySelection(
        MaskedHistory(
            turns=tuple(reversed(selected)),
            omitted_turns=len(turns) - len(selected),
        ),
        measurements,
    )


def _select_priority_turns(
    turns: tuple[MaskedHistoryTurn, ...],
    measurements: TokenMeasurements,
    token_limit: int,
) -> tuple[tuple[MaskedHistoryTurn, ...], TokenMeasurements]:
    protected_index = next(
        (index for index, turn in enumerate(turns) if turn.is_completed),
        None,
    )
    if protected_index is None:
        return _largest_fitting_prefix(
            turns, lambda prefix: prefix, measurements, token_limit
        )
    protected = turns[protected_index]
    newer = turns[:protected_index]
    fitting_newer, measurements = _largest_fitting_prefix(
        newer,
        lambda prefix: (*prefix, protected),
        measurements,
        token_limit,
    )
    selected = (*fitting_newer, protected)
    if fitting_newer != newer:
        return selected, measurements
    older = turns[protected_index + 1 :]
    fitting_older, measurements = _largest_fitting_prefix(
        older,
        lambda prefix: (*selected, *prefix),
        measurements,
        token_limit,
    )
    return (*selected, *fitting_older), measurements


def _masked_turn(source_turn: SavedHistoryTurn) -> MaskedHistoryTurn:
    return MaskedHistoryTurn(
        source_turn.user_content,
        source_turn.assistant_content,
        source_turn.is_completed,
    )


def _flush_pending(
    selected: tuple[MaskedHistoryTurn, ...],
    pending: tuple[MaskedHistoryTurn, ...],
    measurements: TokenMeasurements,
    token_limit: int,
) -> tuple[tuple[MaskedHistoryTurn, ...], int, bool, TokenMeasurements]:
    if not pending:
        return selected, 0, False, measurements
    candidate = (*selected, *pending)
    measured = _count_turns(candidate, measurements)
    if measured.count <= token_limit:
        return candidate, 0, False, measured.measurements
    fitting, measurements = _largest_fitting_prefix(
        pending,
        lambda prefix: (*selected, *prefix),
        measured.measurements,
        token_limit,
    )
    return (*selected, *fitting), len(pending) - len(fitting), True, measurements


def _retain_latest_completed(
    newer: tuple[MaskedHistoryTurn, ...],
    latest_completed: MaskedHistoryTurn,
    measurements: TokenMeasurements,
    token_limit: int,
) -> tuple[tuple[MaskedHistoryTurn, ...], int, bool, TokenMeasurements]:
    candidate = (*newer, latest_completed)
    measured = _count_turns(candidate, measurements)
    if measured.count <= token_limit:
        return candidate, 0, False, measured.measurements
    fitting_newer, measurements = _largest_fitting_prefix(
        newer,
        lambda prefix: (*prefix, latest_completed),
        measured.measurements,
        token_limit,
    )
    return (
        (*fitting_newer, latest_completed),
        len(newer) - len(fitting_newer),
        True,
        measurements,
    )


def _largest_fitting_prefix(
    candidates: tuple[MaskedHistoryTurn, ...],
    combine: Callable[
        [tuple[MaskedHistoryTurn, ...]],
        tuple[MaskedHistoryTurn, ...],
    ],
    measurements: TokenMeasurements,
    token_limit: int,
) -> tuple[tuple[MaskedHistoryTurn, ...], TokenMeasurements]:
    if not candidates:
        return (), measurements
    complete = _count_turns(combine(candidates), measurements)
    measurements = complete.measurements
    if complete.count <= token_limit:
        return candidates, measurements
    lower = 0
    upper = len(candidates)
    current = measurements
    while lower + 1 < upper:
        middle = (lower + upper) // 2
        measured = _count_turns(combine(candidates[:middle]), current)
        current = measured.measurements
        if measured.count <= token_limit:
            lower = middle
        else:
            upper = middle
    return candidates[:lower], current


def _count_turns(
    turns: tuple[MaskedHistoryTurn, ...],
    measurements: TokenMeasurements,
) -> TokenMeasurement:
    messages = tuple(
        message for turn in reversed(turns) for message in turn_messages(turn)
    )
    return measurements.measure(messages)

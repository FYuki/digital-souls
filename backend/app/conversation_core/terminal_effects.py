from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .models import CoreEvent, ResponseState, TerminalOutcome


def _terminal_event_type(state: ResponseState) -> str:
    return {
        ResponseState.COMPLETED: "response_completed",
        ResponseState.CANCELLED: "response_cancelled",
        ResponseState.FAILED: "response_failed",
        ResponseState.PRIVACY_SKIPPED: "response_privacy_skipped",
    }[state]


async def run_terminal_effects(
    outcome: TerminalOutcome,
    *,
    session_id: str,
    response_start_event: asyncio.Event,
    persist: Callable[[TerminalOutcome], Awaitable[None]],
    deliver: Callable[[CoreEvent], Awaitable[None]],
    start_pending: Callable[[], Awaitable[None]],
) -> None:
    await response_start_event.wait()
    event = CoreEvent(
        type=_terminal_event_type(outcome.state),
        session_id=session_id,
        response_id=outcome.response_id,
        generation=outcome.generation,
        reason=outcome.reason,
        source_utterance_ids=outcome.source_utterance_ids,
        source_inputs=outcome.source_inputs,
        last_text_sequence=outcome.last_text_sequence,
        last_audio_sequence=len(outcome.audio_segments),
        terminal_state_bounds_ns=outcome.terminal_state_bounds_ns,
    )
    try:
        try:
            await persist(outcome)
        finally:
            # 保存失敗を保持しつつ、配送失敗があれば配送失敗を主例外にする。
            await deliver(event)
    finally:
        await start_pending()

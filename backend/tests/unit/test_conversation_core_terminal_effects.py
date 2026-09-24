from __future__ import annotations

import asyncio
import importlib

import pytest

from app.conversation_core.models import ResponseState, TerminalOutcome
from app.conversation_core.session import DeliveryError


def _terminal_effects_module():
    try:
        return importlib.import_module("app.conversation_core.terminal_effects")
    except ModuleNotFoundError as error:
        if error.name == "app.conversation_core.terminal_effects":
            pytest.fail("Conversation Core terminal effects boundary is not implemented")
        raise


def _outcome() -> TerminalOutcome:
    return TerminalOutcome(
        response_id="response-1",
        generation=1,
        state=ResponseState.COMPLETED,
        reason=None,
        generated_text="結果",
        audio_segments=(),
        last_played_audio_sequence=0,
        last_text_sequence=1,
        source_utterance_ids=("utterance-1",),
    )


def test_terminal_effects_persist_deliver_and_start_pending_in_order():
    async def scenario() -> None:
        module = _terminal_effects_module()
        started = asyncio.Event()
        calls: list[str] = []

        async def persist(_outcome: TerminalOutcome) -> None:
            calls.append("persist")

        async def deliver(_event: object) -> None:
            calls.append("deliver")

        async def start_pending() -> None:
            calls.append("start_pending")

        started.set()
        await module.run_terminal_effects(
            _outcome(),
            session_id="session-1",
            response_start_event=started,
            persist=persist,
            deliver=deliver,
            start_pending=start_pending,
        )

        assert calls == ["persist", "deliver", "start_pending"]

    asyncio.run(scenario())


def test_terminal_effects_continue_after_persistence_failure():
    async def scenario() -> None:
        module = _terminal_effects_module()
        started = asyncio.Event()
        started.set()
        calls: list[str] = []

        async def persist(_outcome: TerminalOutcome) -> None:
            calls.append("persist")
            raise RuntimeError("persistence sentinel")

        async def deliver(_event: object) -> None:
            calls.append("deliver")

        async def start_pending() -> None:
            calls.append("start_pending")

        with pytest.raises(RuntimeError, match="persistence sentinel"):
            await module.run_terminal_effects(
                _outcome(),
                session_id="session-1",
                response_start_event=started,
                persist=persist,
                deliver=deliver,
                start_pending=start_pending,
            )

        assert calls == ["persist", "deliver", "start_pending"]

    asyncio.run(scenario())


def test_terminal_effects_keep_delivery_failure_as_primary_when_both_fail():
    async def scenario() -> None:
        module = _terminal_effects_module()
        started = asyncio.Event()
        started.set()
        calls: list[str] = []

        async def persist(_outcome: TerminalOutcome) -> None:
            calls.append("persist")
            raise RuntimeError("persistence sentinel")

        async def deliver(_event: object) -> None:
            calls.append("deliver")
            raise DeliveryError("delivery sentinel")

        async def start_pending() -> None:
            calls.append("start_pending")

        with pytest.raises(DeliveryError, match="delivery sentinel") as raised:
            await module.run_terminal_effects(
                _outcome(),
                session_id="session-1",
                response_start_event=started,
                persist=persist,
                deliver=deliver,
                start_pending=start_pending,
            )

        assert calls == ["persist", "deliver", "start_pending"]
        assert isinstance(raised.value.__context__, RuntimeError)

    asyncio.run(scenario())

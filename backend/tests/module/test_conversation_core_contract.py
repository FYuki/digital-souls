from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from tests.conversation_core_test_support import (
    BlockingLlm,
    RecordingDelivery,
    RecordingObservation,
    RecordingPersistence,
    RecordingStt,
    RecordingTts,
    event_field,
    response_id_factory,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = REPOSITORY_ROOT / "contracts" / "voice-session" / "fixtures"
CONTRACT_FIXTURES = (
    "normal.json",
    "cancel-race.json",
    "duplicate.json",
    "out-of-order.json",
    "reconnect.json",
)


def _fixture(name: str) -> dict[str, object]:
    value = json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _core_module():
    try:
        return importlib.import_module("app.conversation_core")
    except ModuleNotFoundError as error:
        if error.name == "app.conversation_core":
            pytest.fail("app.conversation_core must implement the shared fixture contract")
        raise


def _session(session_id: str, *response_ids: str):
    module = _core_module()
    delivery = RecordingDelivery()
    persistence = RecordingPersistence()
    session = module.ConversationCoreSession(
        session_id=session_id,
        response_id_factory=response_id_factory(*response_ids),
        delivery=delivery,
        persistence=persistence,
        observation=RecordingObservation(),
        stt=RecordingStt(),
        llm=BlockingLlm(),
        tts=RecordingTts(),
    )
    return module, session, delivery, persistence


def _events(fixture: dict[str, object]) -> list[dict[str, object]]:
    events = fixture["events"]
    assert isinstance(events, list)
    assert all(isinstance(event, dict) for event in events)
    return events


@pytest.mark.parametrize("fixture_name", CONTRACT_FIXTURES)
def test_core_contract_uses_each_shared_fixture_as_its_input(fixture_name: str) -> None:
    fixture = _fixture(fixture_name)

    assert _events(fixture)
    assert isinstance(fixture.get("expected"), dict)


def test_normal_fixture_groups_pending_utterances_into_one_response() -> None:
    fixture = _fixture("normal.json")
    events = _events(fixture)
    expected = fixture["expected"]
    assert isinstance(expected, dict)
    finalized = [event for event in events if event["type"] == "utterance_finalized"]
    started = next(event for event in events if event["type"] == "response_started")

    async def exercise() -> None:
        _module, session, delivery, persistence = _session(
            str(started["session_id"]),
            str(started["response_id"]),
        )
        first = await session.finalize_utterance(
            utterance_id=str(finalized[0]["utterance_id"]),
            transcript=str(finalized[0]["transcript"]),
            should_response=bool(finalized[0]["should_response"]),
        )
        second = await session.finalize_utterance(
            utterance_id=str(finalized[1]["utterance_id"]),
            transcript=str(finalized[1]["transcript"]),
            should_response=bool(finalized[1]["should_response"]),
        )

        assert first is None
        assert list(second.source_utterance_ids) == expected[
            "response_source_utterance_ids"
        ]
        response_started = next(
            event
            for event in delivery.events
            if event_field(event, "type") == "response_started"
        )
        assert list(event_field(response_started, "source_utterance_ids")) == expected[
            "response_source_utterance_ids"
        ]
        assert len(persistence.starts) == 1
        persisted_response_id, user_content = persistence.starts[0]
        assert persisted_response_id == str(started["response_id"])
        transcript_positions = [
            user_content.index(str(event["transcript"])) for event in finalized
        ]
        assert transcript_positions == sorted(transcript_positions)

    asyncio.run(exercise())


def test_cancel_race_fixture_drops_the_late_delta() -> None:
    fixture = _fixture("cancel-race.json")
    events = _events(fixture)
    expected = fixture["expected"]
    assert isinstance(expected, dict)
    started = events[0]
    early_delta = events[1]
    cancelled = events[2]
    late_delta = events[3]
    source_id = started["source_utterance_ids"][0]

    async def exercise() -> None:
        _module, session, delivery, persistence = _session(
            str(started["session_id"]),
            str(started["response_id"]),
        )
        response = await session.finalize_utterance(
            utterance_id=str(source_id),
            transcript="fixture source",
            should_response=True,
        )
        assert await session.accept_text_delta(
            response_id=response.response_id,
            generation=response.generation,
            text_sequence=int(early_delta["text_sequence"]),
            text=str(early_delta["text"]),
            text_range=(
                int(early_delta["text_range"]["start"]),
                int(early_delta["text_range"]["end"]),
            ),
        )
        await session.cancel_response(
            response_id=response.response_id,
            reason=str(cancelled["reason"]),
        )
        accepted = await session.accept_text_delta(
            response_id=response.response_id,
            generation=response.generation,
            text_sequence=int(late_delta["text_sequence"]),
            text=str(late_delta["text"]),
            text_range=(
                int(late_delta["text_range"]["start"]),
                int(late_delta["text_range"]["end"]),
            ),
        )

        assert accepted is False
        terminal = [
            event_field(event, "type")
            for event in delivery.events
            if event_field(event, "type").startswith("response_")
            and event_field(event, "type") in {
                "response_completed",
                "response_cancelled",
                "response_failed",
            }
        ]
        assert terminal == [expected["terminal_event"]]
        assert len(persistence.outcomes) == 1

    asyncio.run(exercise())


def test_duplicate_fixture_applies_a_delta_once() -> None:
    fixture = _fixture("duplicate.json")
    events = _events(fixture)
    expected = fixture["expected"]
    assert isinstance(expected, dict)
    started, first_delta, duplicate_delta = events
    source_id = started["source_utterance_ids"][0]

    async def exercise() -> None:
        _module, session, delivery, _persistence = _session(
            str(started["session_id"]),
            str(started["response_id"]),
        )
        response = await session.finalize_utterance(
            utterance_id=str(source_id),
            transcript="fixture source",
            should_response=True,
        )
        for delta in (first_delta, duplicate_delta):
            await session.accept_text_delta(
                response_id=response.response_id,
                generation=response.generation,
                event_id=str(delta["event_id"]),
                text_sequence=int(delta["text_sequence"]),
                text=str(delta["text"]),
                text_range=(
                    int(delta["text_range"]["start"]),
                    int(delta["text_range"]["end"]),
                ),
            )

        deltas = [
            event
            for event in delivery.events
            if event_field(event, "type") == "response_delta"
        ]
        assert [event_field(event, "text") for event in deltas] == [
            expected["assistant_text"]
        ]

    asyncio.run(exercise())


def test_duplicate_fixture_rejects_a_conflicting_payload_without_side_effects() -> None:
    fixture = _fixture("duplicate.json")
    events = _events(fixture)
    conflicting = fixture["conflicting_event"]
    expected = fixture["expected"]
    assert isinstance(conflicting, dict)
    assert isinstance(expected, dict)
    started, first_delta, _duplicate_delta = events
    source_id = started["source_utterance_ids"][0]

    async def exercise() -> None:
        module, session, delivery, persistence = _session(
            str(started["session_id"]),
            str(started["response_id"]),
        )
        response = await session.finalize_utterance(
            utterance_id=str(source_id),
            transcript="fixture source",
            should_response=True,
        )
        await session.accept_text_delta(
            response_id=response.response_id,
            generation=response.generation,
            event_id=str(first_delta["event_id"]),
            text_sequence=int(first_delta["text_sequence"]),
            text=str(first_delta["text"]),
            text_range=(
                int(first_delta["text_range"]["start"]),
                int(first_delta["text_range"]["end"]),
            ),
        )
        delivered_before_conflict = list(delivery.events)
        persisted_before_conflict = list(persistence.outcomes)

        try:
            await session.accept_text_delta(
                response_id=response.response_id,
                generation=response.generation,
                event_id=str(conflicting["event_id"]),
                text_sequence=int(conflicting["text_sequence"]),
                text=str(conflicting["text"]),
                text_range=(
                    int(conflicting["text_range"]["start"]),
                    int(conflicting["text_range"]["end"]),
                ),
            )
        except module.TerminalProtocolError:
            classification = "terminal_protocol_error"
        else:
            classification = "accepted"

        assert classification == expected["conflicting_duplicate"]
        assert delivery.events == delivered_before_conflict
        assert persistence.outcomes == persisted_before_conflict

    asyncio.run(exercise())


def test_out_of_order_fixture_does_not_advance_across_a_gap() -> None:
    fixture = _fixture("out-of-order.json")
    events = _events(fixture)
    expected = fixture["expected"]
    assert isinstance(expected, dict)
    response_id = str(events[0]["response_id"])
    session_id = str(events[0]["session_id"])

    async def exercise() -> None:
        _module, session, delivery, _persistence = _session(session_id, response_id)
        response = await session.finalize_utterance(
            utterance_id="30000000-0000-4000-8000-000000000520",
            transcript="fixture source",
            should_response=True,
        )
        accepted: list[bool] = []
        for delta in events:
            accepted.append(
                await session.accept_text_delta(
                    response_id=response.response_id,
                    generation=response.generation,
                    event_id=str(delta["event_id"]),
                    text_sequence=int(delta["text_sequence"]),
                    text=str(delta["text"]),
                    text_range=(
                        int(delta["text_range"]["start"]),
                        int(delta["text_range"]["end"]),
                    ),
                )
            )

        sequences = [
            event_field(event, "text_sequence")
            for event in delivery.events
            if event_field(event, "type") == "response_delta"
        ]
        assert accepted == [True, False, True]
        assert sequences == [1, 2]
        assert sequences[0] == expected["last_contiguous_sequence_before_recovery"]

    asyncio.run(exercise())


def test_reconnect_fixture_keeps_session_but_rejects_the_old_response_generation() -> None:
    fixture = _fixture("reconnect.json")
    events = _events(fixture)
    expected = fixture["expected"]
    assert isinstance(expected, dict)
    started = next(event for event in events if event["type"] == "response_started")
    old_delta = next(
        event
        for event in events
        if event["type"] == "response_delta" and event["text_sequence"] == 1
    )
    late_delta = next(
        event
        for event in events
        if event["type"] == "response_delta" and event["text_sequence"] == 2
    )

    async def exercise() -> None:
        module, session, delivery, persistence = _session(
            str(started["session_id"]),
            str(started["response_id"]),
            "53000000-0000-4000-8000-000000000002",
        )
        response = await session.finalize_utterance(
            utterance_id=str(started["source_utterance_ids"][0]),
            transcript="fixture source",
            should_response=True,
        )
        await session.accept_text_delta(
            response_id=response.response_id,
            generation=response.generation,
            event_id=str(old_delta["event_id"]),
            text_sequence=int(old_delta["text_sequence"]),
            text=str(old_delta["text"]),
            text_range=(0, 2),
        )
        await session.disconnect()
        await session.reconnect()
        accepted = await session.accept_text_delta(
            response_id=response.response_id,
            generation=response.generation,
            event_id=str(late_delta["event_id"]),
            text_sequence=int(late_delta["text_sequence"]),
            text=str(late_delta["text"]),
            text_range=(2, 4),
        )
        new_response = await session.finalize_utterance(
            utterance_id=str(expected["new_utterance_id"]),
            transcript="reconnected source",
            should_response=True,
        )

        assert session.session_id == expected["session_id_preserved"]
        assert session.response(response.response_id).state is module.ResponseState.CANCELLED
        assert accepted is False
        assert new_response.response_id != response.response_id
        assert len(persistence.outcomes) == 1
        delivered_texts = [
            event_field(event, "text")
            for event in delivery.events
            if event_field(event, "type") == "response_delta"
        ]
        assert delivered_texts == [old_delta["text"]]

    asyncio.run(exercise())

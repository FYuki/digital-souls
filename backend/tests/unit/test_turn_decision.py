from __future__ import annotations

import asyncio

from app.conversation_core.models import ResponseState, UtteranceState
from app.conversation_core.session import ConversationCoreSession
from app.conversation_core.turn_decision import classify_turn
from tests.conversation_core_test_support import (
    BlockingLlm,
    RecordingDelivery,
    RecordingObservation,
    RecordingPersistence,
    RecordingStt,
    RecordingTts,
    response_id_factory,
)


SESSION_ID = "20000000-0000-4000-8000-000000000201"
OLD_RESPONSE_ID = "50000000-0000-4000-8000-000000000201"
NEW_RESPONSE_ID = "50000000-0000-4000-8000-000000000202"
OLD_UTTERANCE_ID = "30000000-0000-4000-8000-000000000201"
INTERRUPTION_ID = "30000000-0000-4000-8000-000000000202"


def test_classify_turn_limits_backchannel_to_complete_short_reactions() -> None:
    assert classify_turn("うん。") == "backchannel"
    assert classify_turn("うん、うん") == "backchannel"
    assert classify_turn("なるほど") == "backchannel"
    assert classify_turn("うん、でも別の質問があります") == "take_turn"
    assert classify_turn("はい、続きを止めて") == "take_turn"
    assert classify_turn("  ") == "indeterminate"


def test_backchannel_keeps_active_response_and_is_not_carried_to_next_prompt() -> None:
    async def exercise() -> None:
        delivery = RecordingDelivery()
        session = ConversationCoreSession(
            session_id=SESSION_ID,
            response_id_factory=response_id_factory(OLD_RESPONSE_ID, NEW_RESPONSE_ID),
            delivery=delivery,
            persistence=RecordingPersistence(),
            observation=RecordingObservation(),
            stt=RecordingStt("うん"),
            llm=BlockingLlm(),
            tts=RecordingTts(),
        )
        old_response = await session.finalize_utterance(
            utterance_id=OLD_UTTERANCE_ID,
            transcript="説明して",
            should_response=True,
        )
        assert old_response is not None

        result = await session.start_transcription(
            utterance_id=INTERRUPTION_ID,
            audio=b"backchannel",
            should_response=True,
            interrupted_response_id=OLD_RESPONSE_ID,
        )

        assert result is None
        assert session.active_response is not None
        assert session.active_response.response_id == OLD_RESPONSE_ID
        assert session.utterance(INTERRUPTION_ID).state is UtteranceState.CONSUMED
        decisions = [event for event in delivery.events if event.type == "turn_decision"]
        assert [(event.decision, event.final) for event in decisions] == [
            ("backchannel", True)
        ]
        await session.end()

    asyncio.run(exercise())


def test_take_turn_cancels_active_response_before_starting_new_response() -> None:
    async def exercise() -> None:
        delivery = RecordingDelivery()
        session = ConversationCoreSession(
            session_id=SESSION_ID,
            response_id_factory=response_id_factory(OLD_RESPONSE_ID, NEW_RESPONSE_ID),
            delivery=delivery,
            persistence=RecordingPersistence(),
            observation=RecordingObservation(),
            stt=RecordingStt("それより別の質問です"),
            llm=BlockingLlm(),
            tts=RecordingTts(),
        )
        old_response = await session.finalize_utterance(
            utterance_id=OLD_UTTERANCE_ID,
            transcript="説明して",
            should_response=True,
        )
        assert old_response is not None

        new_response = await session.start_transcription(
            utterance_id=INTERRUPTION_ID,
            audio=b"take-turn",
            should_response=True,
            interrupted_response_id=OLD_RESPONSE_ID,
        )

        assert new_response is not None
        assert new_response.response_id == NEW_RESPONSE_ID
        assert session.response(OLD_RESPONSE_ID).state is ResponseState.CANCELLED
        event_types = [event.type for event in delivery.events]
        assert event_types.index("turn_decision") < event_types.index(
            "response_cancelled"
        ) < event_types.index("utterance_finalized") < event_types.index(
            "response_started", 1
        )
        await session.end()

    asyncio.run(exercise())

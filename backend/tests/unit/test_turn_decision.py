from __future__ import annotations

import asyncio

import pytest

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
    assert classify_turn("そうなんですね。") == "backchannel"
    assert classify_turn("そうですね！") == "backchannel"
    assert classify_turn("そうですね、でも質問があります") == "take_turn"
    assert classify_turn("そうなんですね、具体例を教えて") == "take_turn"
    assert classify_turn("うん、でも別の質問があります") == "take_turn"
    assert classify_turn("はい、続きを止めて") == "take_turn"
    assert classify_turn("  ") == "indeterminate"


@pytest.mark.parametrize("transcript, expected_decision", [
    ("うん", "backchannel"), ("そうなんですね", "backchannel"), ("そうですね", "backchannel"),
    ("ヘッ", "indeterminate"), ("ん", "indeterminate"),
])
def test_non_taking_reaction_keeps_active_response_and_is_not_carried_to_next_prompt(
    transcript: str, expected_decision: str,
) -> None:
    async def exercise() -> None:
        delivery = RecordingDelivery()
        session = ConversationCoreSession(
            session_id=SESSION_ID,
            response_id_factory=response_id_factory(OLD_RESPONSE_ID, NEW_RESPONSE_ID),
            delivery=delivery,
            persistence=RecordingPersistence(),
            observation=RecordingObservation(),
            stt=RecordingStt(transcript),
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
            (expected_decision, True)
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


@pytest.mark.parametrize("transcript", ["ウン。", "ｳﾝｳﾝ", "ハイ", "ヘー", "ナルホド", "ソウナンデスネ"])
def test_backchannel_accepts_kana_and_width_variants(transcript: str) -> None:
    assert classify_turn(transcript) == "backchannel"


@pytest.mark.parametrize("transcript", ["ん", "ヘッ", "へっ", "エッ", "あっ", "おー", "ふっ", "ほっ"])
def test_short_nonlexical_reaction_remains_indeterminate(transcript: str) -> None:
    assert classify_turn(transcript) == "indeterminate"


@pytest.mark.parametrize("transcript", [
    "ん？", "ヘッ?", "エッ？", "あっ、止めて", "ん、それは違う", "ヘッ、もう一度教えて",
    "ハイ、続きを止めて", "ウン、でも質問です", "へ", "ほ", "ふ", "んー、待って",
    "ストップ", "やめて", "違う", "はい？何ですか", "あっという間", "おーい",
])
def test_short_reaction_does_not_hide_question_or_following_instruction(transcript: str) -> None:
    assert classify_turn(transcript) == "take_turn"


@pytest.mark.parametrize("transcript", ["ん", "ヘッ", "エッ"])
def test_indeterminate_reaction_preserves_output_until_following_take_turn(transcript: str) -> None:
    async def exercise() -> None:
        delivery = RecordingDelivery()
        stt = RecordingStt(transcript)
        session = ConversationCoreSession(
            session_id=SESSION_ID,
            response_id_factory=response_id_factory(OLD_RESPONSE_ID, NEW_RESPONSE_ID),
            delivery=delivery,
            persistence=RecordingPersistence(),
            observation=RecordingObservation(),
            stt=stt,
            llm=BlockingLlm(),
            tts=RecordingTts(),
        )
        try:
            await session.finalize_utterance(utterance_id=OLD_UTTERANCE_ID, transcript="説明して", should_response=True)
            decision = await session.preview_turn(utterance_id=INTERRUPTION_ID, audio=b"preview", interrupted_response_id=OLD_RESPONSE_ID)
            assert decision == "indeterminate"
            assert session.response(OLD_RESPONSE_ID).state is not ResponseState.CANCELLED
            assert not any(event.type == "response_cancelled" for event in delivery.events)
            stt.transcript = transcript + "、止めて。質問があります"
            result = await session.start_transcription(utterance_id=INTERRUPTION_ID, audio=b"final", should_response=True, interrupted_response_id=OLD_RESPONSE_ID)
            assert result is not None
            assert session.response(OLD_RESPONSE_ID).state is ResponseState.CANCELLED
            assert [(event.decision, event.final) for event in delivery.events if event.type == "turn_decision"] == [("indeterminate", False), ("take_turn", True)]
        finally:
            await session.end()

    asyncio.run(exercise())

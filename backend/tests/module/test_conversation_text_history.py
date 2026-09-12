from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from uuid import uuid4

from app.conversation_core import ConversationCoreSession, InputSource, TextDelta
from app.conversation_core.adapters import ConversationHistoryPersistenceAdapter
from app.conversation_history.service import ConversationHistorySession
from app.livekit_transport.delivery import decode_core_event
from app.livekit_transport.production import _ConversationCoreDelivery
from app.livekit_transport.text_input import TextInputReceiver
from app.privacy.contracts import (
    ConversationHistoryAction, ConversationHistoryDecision, HistoryDecisionReasonCode,
)
from tests.conversation_core_test_support import RecordingDelivery, RecordingObservation, RecordingStt, RecordingTts
from tests.conversation_history_test_support import create_repository


def test_speech_text_speech_share_sqlite_history_context_and_tts(tmp_path: Path) -> None:
    async def run():
        repository = create_repository(tmp_path / "mixed.db", uuid_factory=uuid4)
        conversation = repository.create_conversation("miori")
        sanitizer = MagicMock()

        def sanitize(text):
            return ConversationHistoryDecision(
                action=ConversationHistoryAction.STORE_MASKED,
                reason_code=HistoryDecisionReasonCode.MASKED,
                sanitizer_version="test", policy_version="test", content=text,
            )

        sanitizer.sanitize_current_user.side_effect = sanitize
        sanitizer.sanitize_assistant.side_effect = sanitize
        history = ConversationHistorySession("miori", conversation.conversation_id, repository, sanitizer)
        completed = []
        persistence = ConversationHistoryPersistenceAdapter(
            history_session=history, completed_turn_observer=completed.append,
        )
        contexts = []

        class ContextLlm:
            async def generate(self, text):
                contexts.append(tuple(history.prompt_turns(max_completed_turns=10, page_size=10)))
                reply = f"回答:{text}。"
                yield TextDelta(1, reply, (0, len(reply)))

        delivery, stt, tts = RecordingDelivery(), RecordingStt(), RecordingTts()
        session = ConversationCoreSession(
            session_id=str(uuid4()), response_id_factory=lambda: str(uuid4()),
            delivery=delivery, persistence=persistence, observation=RecordingObservation(),
            stt=stt, llm=ContextLlm(), tts=tts,
        )
        ids = [str(uuid4()) for _ in range(3)]
        inputs = ["最初の音声", "途中のテキスト", "次の音声"]
        receipts = []

        async def accept_text(input_id, text):
            response = await session.submit_text(input_id=input_id, text=text)
            return response.response_id if response is not None else None

        async def publish_receipt(event):
            receipts.append(event)

        participant_id = str(uuid4())
        receiver = TextInputReceiver(
            session_id=session.session_id, participant_id=participant_id,
            submit=accept_text, publish=publish_receipt, accepting_input=lambda: session.accepting_input,
        )
        for index, text in enumerate(inputs):
            if index == 1:
                submitted = {
                    "type": "user_text_submitted", "protocol_version": "1.1",
                    "event_id": ids[index], "session_id": session.session_id,
                    "monotonic_timestamp_ms": 1, "text": text,
                    "speaker": {"role": "user", "participant_id": participant_id},
                }
                await receiver.receive(submitted)
                await receiver.receive(submitted)
            else:
                stt.transcript = text
                await session.start_transcription(utterance_id=ids[index], audio=text.encode(), should_response=True)
            async with asyncio.timeout(3):
                while len(completed) <= index:
                    await asyncio.sleep(0.001)

        turns = repository.list_turns("miori", conversation.conversation_id)
        assert {turn.user_content for turn in turns} == set(inputs)
        assert all(turn.conversation_id == conversation.conversation_id for turn in turns)
        assert {turn.assistant_content for turn in turns} == {f"回答:{text}。" for text in inputs}
        assert [len(context) for context in contexts] == [0, 1, 2]
        assert {turn.user_content for turn in contexts[-1]} == set(inputs[:2])
        assert stt.calls == [inputs[0].encode(), inputs[2].encode()]
        assert tts.calls == [f"回答:{text}。" for text in inputs]
        assert len(completed) == 3
        assert [receipt["status"] for receipt in receipts] == ["accepted", "accepted"]
        assert receipts[0]["response_id"] == receipts[1]["response_id"]
        started = [event for event in delivery.events if event.type == "response_started"]
        assert started[1].source_inputs == (InputSource(ids[1], "text"),)
        assert started[1].source_utterance_ids == ()
        assert {event.utterance_id for event in delivery.events if event.type == "utterance_finalized"} == {ids[0], ids[2]}

        # 実運用のprotocol serializerでも直接テキストを音声IDへ偽装しない。
        serializer = object.__new__(_ConversationCoreDelivery)
        serializer._character_speaker = {"role": "character", "participant_id": str(uuid4()), "character_id": "miori"}
        wire = decode_core_event(serializer._voice_payload(started[1]))
        assert wire["source_inputs"] == [{"input_id": ids[1], "source": "text"}]
        assert wire["source_utterance_ids"] == []
        assert wire["history_turn_id"] in {str(turn.turn_id) for turn in turns}
        await session.end()
    asyncio.run(run())


@pytest.mark.parametrize("source", ["text", "speech"])
def test_privacy_skip_reaches_wire_without_content_and_keeps_one_history_turn(tmp_path: Path, source: str) -> None:
    async def run():
        repository = create_repository(tmp_path / "privacy.db", uuid_factory=uuid4)
        conversation = repository.create_conversation("miori")
        sanitizer = MagicMock()
        sanitizer.sanitize_current_user.return_value = ConversationHistoryDecision(
            action=ConversationHistoryAction.SKIP_CONTENT,
            reason_code=HistoryDecisionReasonCode.STORAGE_OPT_OUT,
            sanitizer_version="test", policy_version="test", content=None,
        )
        history = ConversationHistorySession("miori", conversation.conversation_id, repository, sanitizer)
        coordinator, audio = MagicMock(), MagicMock()
        wire = []

        async def send_core(payload):
            wire.append(decode_core_event(payload))

        coordinator.send_core = AsyncMock(side_effect=send_core)
        delivery = _ConversationCoreDelivery(
            coordinator=coordinator, audio_source=audio,
            character_participant_id=str(uuid4()), character_id="miori", user_participant_id=str(uuid4()),
        )
        stt, llm, tts = RecordingStt(transcript="保存しない入力"), MagicMock(), RecordingTts()
        session = ConversationCoreSession(
            session_id=str(uuid4()), response_id_factory=lambda: str(uuid4()),
            delivery=delivery, persistence=ConversationHistoryPersistenceAdapter(history_session=history),
            observation=RecordingObservation(), stt=stt, llm=llm, tts=tts,
        )
        input_id = str(uuid4())
        if source == "text":
            await session.submit_text(input_id=input_id, text="保存しない入力")
            await session.submit_text(input_id=input_id, text="保存しない入力")
        else:
            await session.start_transcription(utterance_id=input_id, audio=b"speech", should_response=True)
        async with asyncio.timeout(3):
            while not any(event["type"] == "response_privacy_skipped" for event in wire):
                await asyncio.sleep(0.001)
        privacy = [event for event in wire if event["type"] == "response_privacy_skipped"]
        assert len(privacy) == 1
        assert privacy[0]["source_inputs"] == [{"input_id": input_id, "source": source}]
        assert not {"text", "transcript", "user_content", "assistant_content"} & privacy[0].keys()
        assert not any(event["type"].startswith("response_") and event["type"] != "response_privacy_skipped" for event in wire)
        assert [event["utterance_id"] for event in wire if event["type"] == "utterance_discarded"] == ([input_id] if source == "speech" else [])
        turns = repository.list_turns("miori", conversation.conversation_id)
        assert len(turns) == 1
        assert turns[0].user_content is None and turns[0].assistant_content is None
        assert len(stt.calls) == (1 if source == "speech" else 0)
        llm.generate.assert_not_called()
        assert tts.calls == []
        audio.begin_response.assert_not_called()
        await session.end()
    asyncio.run(run())

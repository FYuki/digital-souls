from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.conversation_core import ConversationCoreSession, InputSource, TerminalProtocolError
from tests.conversation_core_test_support import (
    BlockingLlm, RecordingDelivery, RecordingObservation, RecordingPersistence,
    RecordingStt, RecordingTts,
)


def _session(*, content_skipped=False):
    delivery, persistence = RecordingDelivery(), RecordingPersistence(content_skipped=content_skipped)
    stt, llm, tts = RecordingStt(), BlockingLlm(), RecordingTts()
    session = ConversationCoreSession(
        session_id=str(uuid4()), response_id_factory=lambda: str(uuid4()),
        delivery=delivery, persistence=persistence, observation=RecordingObservation(),
        stt=stt, llm=llm, tts=tts,
    )
    return session, delivery, persistence, stt, llm, tts


def test_invalidated_preview_cannot_cancel_current_response_after_stt_returns() -> None:
    async def run():
        session, delivery, _, _, _, _ = _session()
        started, release = asyncio.Event(), asyncio.Event()

        class DelayedStt:
            async def transcribe(self, audio: bytes) -> str:
                started.set()
                await release.wait()
                return "別の話をしましょう"

        session._stt = DelayedStt()
        response = await session.submit_text(input_id=str(uuid4()), text="継続する回答")
        current = True
        preview = asyncio.create_task(session.preview_turn(
            utterance_id=str(uuid4()), audio=b"pcm", interrupted_response_id=response.response_id,
            input_is_current=lambda: current,
        ))
        await started.wait()
        current = False
        release.set()
        await preview
        assert not session.response(response.response_id).state.is_terminal
        assert not any(event.type == "turn_decision" for event in delivery.events)
        await session.end()

    asyncio.run(run())


def test_direct_text_uses_common_response_without_synthetic_speech() -> None:
    async def run():
        session, delivery, persistence, stt, llm, _ = _session()
        input_id = str(uuid4())
        response = await session.submit_text(input_id=input_id, text="直接テキスト")
        duplicate = await session.submit_text(input_id=input_id, text="直接テキスト")
        assert response == duplicate
        assert response.source_inputs == (InputSource(input_id, "text"),)
        assert response.source_utterance_ids == ()
        assert persistence.starts == [(response.response_id, "直接テキスト")]
        assert stt.calls == []
        assert session.user_input(input_id).source == "text"
        with pytest.raises(KeyError):
            session.utterance(input_id)
        assert [event.type for event in delivery.events] == ["response_started"]
        assert delivery.events[0].source_inputs == response.source_inputs
        await session.end()
    asyncio.run(run())


def test_finalized_pending_speech_is_preserved_before_direct_text() -> None:
    async def run():
        session, _, persistence, *_ = _session()
        speech_id, text_id = str(uuid4()), str(uuid4())
        await session.finalize_utterance(
            utterance_id=speech_id, transcript="確定済みの補足", should_response=False,
        )
        response = await session.submit_text(input_id=text_id, text="続けて質問")
        assert persistence.starts == [(response.response_id, "確定済みの補足\n続けて質問")]
        assert response.source_inputs == (InputSource(speech_id, "speech"), InputSource(text_id, "text"))
        assert response.source_utterance_ids == (speech_id,)
        assert session.pending_inputs == ()
        await session.end()
    asyncio.run(run())


@pytest.mark.parametrize("conflict", ["text", "speech"])
def test_common_input_id_cannot_change_content_or_source(conflict) -> None:
    async def run():
        session, _, persistence, *_ = _session()
        input_id = str(uuid4())
        await session.submit_text(input_id=input_id, text="最初の入力")
        with pytest.raises(TerminalProtocolError):
            if conflict == "text":
                await session.submit_text(input_id=input_id, text="異なる本文")
            else:
                await session.finalize_utterance(
                    utterance_id=input_id, transcript="最初の入力", should_response=True,
                )
        assert len(persistence.starts) == 1
        await session.end()
    asyncio.run(run())


def test_text_pending_during_response_is_started_once_after_terminal() -> None:
    async def run():
        session, delivery, persistence, *_ = _session()
        first = await session.submit_text(input_id=str(uuid4()), text="先行入力")
        input_id = str(uuid4())
        assert await session.submit_text(input_id=input_id, text="後続入力") is None
        assert await session.submit_text(input_id=input_id, text="後続入力") is None
        assert len(session.pending_inputs) == 1
        assert session.pending_utterances == ()
        await session.complete_response(response_id=first.response_id, generation=first.generation)
        async with asyncio.timeout(2):
            while len(persistence.starts) < 2:
                await asyncio.sleep(0)
        assert [text for _, text in persistence.starts] == ["先行入力", "後続入力"]
        assert session.active_response.source_inputs == (InputSource(input_id, "text"),)
        await session.end()
    asyncio.run(run())


def test_text_privacy_skip_preserves_existing_persistence_and_provider_boundary() -> None:
    async def run():
        session, delivery, persistence, stt, llm, tts = _session(content_skipped=True)
        response = await session.submit_text(input_id=str(uuid4()), text="保存対象外")
        async with asyncio.timeout(2):
            while not persistence.outcomes:
                await asyncio.sleep(0)
        assert response.state.value == "privacy_skipped"
        assert stt.calls == llm.calls == tts.calls == []
        assert not any(event.type == "utterance_finalized" for event in delivery.events)
        await session.end()
    asyncio.run(run())

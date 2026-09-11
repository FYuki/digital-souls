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


@pytest.mark.parametrize("invalidation", ["focus", "text"])
def test_invalidated_preview_cannot_cancel_current_response_after_stt_returns(invalidation) -> None:
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
        if invalidation == "focus":
            current = False
        else:
            response = await session.submit_text(input_id=str(uuid4()), text="テキスト優先")
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


def test_text_interrupts_current_response_and_duplicate_does_not_interrupt_again() -> None:
    async def run():
        session, delivery, persistence, *_ = _session()
        first = await session.submit_text(input_id=str(uuid4()), text="先行入力")
        input_id = str(uuid4())
        second = await session.submit_text(input_id=input_id, text="後続入力")
        assert second is not None
        assert session.response(first.response_id).state.value == "cancelled"
        assert await session.submit_text(input_id=input_id, text="後続入力") == second
        assert not session.response(second.response_id).state.is_terminal
        assert len(session.pending_inputs) == 0
        assert session.pending_utterances == ()
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


@pytest.mark.parametrize("late_failure", [False, True])
def test_text_does_not_wait_for_stt_and_late_speech_cannot_start_or_cancel_response(late_failure) -> None:
    async def run():
        session, delivery, persistence, *_ = _session()
        started, release = asyncio.Event(), asyncio.Event()

        class SttError(Exception):
            error_code = "invalid_audio"

        class DelayedStt:
            async def transcribe(self, audio: bytes) -> str:
                started.set()
                await release.wait()
                if late_failure:
                    raise SttError()
                return "遅れた音声による割り込み"

        session._stt = DelayedStt()
        previous = await session.submit_text(input_id=str(uuid4()), text="先行回答")
        speech_id = str(uuid4())
        transcription = session.start_transcription(
            utterance_id=speech_id, audio=b"old", should_response=True,
            interrupted_response_id=previous.response_id,
        )
        await started.wait()
        async with asyncio.timeout(1):
            response = await session.submit_text(input_id=str(uuid4()), text="テキストを優先")
        assert not release.is_set()
        assert response is not None
        assert [text for _, text in persistence.starts] == ["先行回答", "テキストを優先"]
        release.set()
        assert await transcription is None
        assert session.user_input(speech_id).state.value == "discarded"
        assert session.user_input(speech_id).discard_reason == "text_priority"
        assert not session.response(response.response_id).state.is_terminal
        assert not any(event.utterance_id == speech_id and event.type in {
            "utterance_finalized", "turn_decision", "error",
        } for event in delivery.events)
        assert len(persistence.starts) == 2
        await session.end()
    asyncio.run(run())


def test_text_preserves_finalized_pending_speech_while_interrupting_response() -> None:
    async def run():
        session, _, persistence, *_ = _session()
        previous = await session.submit_text(input_id=str(uuid4()), text="保存済みの先行入力")
        speech_id, text_id = str(uuid4()), str(uuid4())
        await session.finalize_utterance(utterance_id=speech_id, transcript="確定済みの補足", should_response=False)
        response = await session.submit_text(input_id=text_id, text="新しい質問")
        assert response.source_inputs == (InputSource(speech_id, "speech"), InputSource(text_id, "text"))
        assert [text for _, text in persistence.starts] == ["保存済みの先行入力", "確定済みの補足\n新しい質問"]
        assert session.response(previous.response_id).state.value == "cancelled"
        await session.end()
    asyncio.run(run())

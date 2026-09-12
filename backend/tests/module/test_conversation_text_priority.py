from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from app.conversation_core import ConversationCoreSession
from app.livekit_transport.production import _ConversationCoreBridge
from app.livekit_transport.text_input import TextInputReceiver
from tests.conversation_core_test_support import (
    BlockingLlm, RecordingDelivery, RecordingObservation, RecordingPersistence, RecordingTts,
)


def test_protocol_text_invalidates_queued_audio_once_and_does_not_wait_for_old_stt() -> None:
    async def run() -> None:
        old_pcm, fresh_pcm = b"\x33\x10" * 480, b"\x44\x10" * 480
        old_started, fresh_started = asyncio.Event(), asyncio.Event()
        release_old, release_fresh = asyncio.Event(), asyncio.Event()
        stt_calls: list[bytes] = []

        class DelayedStt:
            async def transcribe(self, audio: bytes) -> str:
                stt_calls.append(audio)
                if audio == old_pcm:
                    old_started.set()
                    await release_old.wait()
                    return "遅れて完了した音声"
                fresh_started.set()
                await release_fresh.wait()
                return "text後の新しい音声"

        persistence, delivery = RecordingPersistence(), RecordingDelivery()
        session = ConversationCoreSession(
            session_id=str(uuid4()), response_id_factory=lambda: str(uuid4()),
            delivery=delivery, persistence=persistence, observation=RecordingObservation(),
            stt=DelayedStt(), llm=BlockingLlm(), tts=RecordingTts(),
        )
        tasks: list[asyncio.Task[None]] = []
        receipts: list[dict[str, object]] = []
        participant_id = str(uuid4())

        async def submit(input_id: str, text: str) -> str | None:
            discarded = bridge.invalidate_unfinalized_audio()
            response = await session.submit_text(input_id=input_id, text=text, discard_speech_ids=discarded)
            return response.response_id if response else None

        async def publish(event: dict[str, object]) -> None:
            receipts.append(event)

        receiver = TextInputReceiver(
            session_id=session.session_id, participant_id=participant_id, submit=submit,
            publish=publish, accepting_input=lambda: session.accepting_input,
        )
        bridge = _ConversationCoreBridge(
            session, lambda op: tasks.append(asyncio.create_task(op)),
            text_input=receiver, media_tail_seconds=0,
        )

        def event(kind: str, **fields: object) -> bytes:
            return json.dumps({
                "protocol_version": "1.1", "type": kind, "session_id": session.session_id,
                "event_id": str(uuid4()), "monotonic_timestamp_ms": 1,
                "speaker": {"role": "user", "participant_id": participant_id}, **fields,
            }).encode()

        async def wait_for(predicate) -> None:
            async with asyncio.timeout(2):
                while not predicate():
                    await asyncio.sleep(0)

        async def speech(utterance_id: str, pcm: bytes, *, stop: bool = True) -> None:
            bridge.notify(event("speech_started", utterance_id=utterance_id))
            bridge.receive_microphone(pcm)
            if stop:
                bridge.notify(event("speech_stopped", utterance_id=utterance_id))
            await asyncio.sleep(0)

        old_id, queued_id, open_id, fresh_id = (str(uuid4()) for _ in range(4))
        try:
            await session.submit_text(input_id=str(uuid4()), text="保存する先行入力")
            await speech(old_id, old_pcm)
            await asyncio.wait_for(old_started.wait(), 2)
            await speech(queued_id, old_pcm)
            await speech(open_id, old_pcm, stop=False)
            submitted = event("user_text_submitted", text="優先テキスト")
            bridge.notify(submitted)
            await wait_for(lambda: bool(receipts))
            assert receipts[0]["status"] == "accepted"
            assert not release_old.is_set()
            assert [text for _, text in persistence.starts] == ["保存する先行入力", "優先テキスト"]
            for utterance_id in (old_id, queued_id, open_id):
                assert session.user_input(utterance_id).discard_reason == "text_priority"

            await speech(fresh_id, fresh_pcm)
            await asyncio.wait_for(fresh_started.wait(), 2)
            bridge.notify(submitted)
            await wait_for(lambda: len(receipts) == 2)
            assert receipts[0]["response_id"] == receipts[1]["response_id"]
            release_old.set()
            await wait_for(lambda: old_id not in session._transcribing_inputs)
            assert len(persistence.starts) == 2
            release_fresh.set()
            await wait_for(lambda: fresh_id in session._inputs)
            assert session.user_input(fresh_id).text == "text後の新しい音声"
            assert session.user_input(fresh_id).discard_reason is None
            assert stt_calls == [old_pcm, fresh_pcm]
            assert not any(event.type == "utterance_finalized" and event.utterance_id in {
                old_id, queued_id, open_id,
            } for event in delivery.events)
        finally:
            release_old.set()
            release_fresh.set()
            await asyncio.gather(*tasks)
            await session.end()

    asyncio.run(run())

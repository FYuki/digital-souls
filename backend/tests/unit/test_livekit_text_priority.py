from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.livekit_transport.production import _ConversationCoreBridge


@pytest.mark.parametrize("old_stage", ["transcription", "preview"])
def test_old_audio_completion_cannot_start_queued_input_while_new_audio_is_running(old_stage) -> None:
    async def run() -> None:
        started: list[str] = []
        releases: dict[str, asyncio.Event] = {}
        tasks: list[asyncio.Task[None]] = []

        async def work(utterance_id: str) -> None:
            started.append(utterance_id)
            await releases.setdefault(utterance_id, asyncio.Event()).wait()

        def transcribe(*, utterance_id: str, **_request: object) -> asyncio.Task[None]:
            task = asyncio.create_task(work(utterance_id))
            tasks.append(task)
            return task

        async def preview(*, utterance_id: str, **_request: object) -> str:
            await work(utterance_id)
            return "backchannel"

        core = SimpleNamespace(start_transcription=transcribe, preview_turn=preview, accepting_input=True)
        bridge = _ConversationCoreBridge(core, lambda op: tasks.append(asyncio.create_task(op)))
        if old_stage == "transcription":
            await bridge._enqueue_user_audio(utterance_id="old", microphone_pcm=b"\x33\x00" * 480)
        else:
            bridge._transcription_active = True
            tasks.append(asyncio.create_task(bridge._preview_user_turn(
                utterance_id="old", interrupted_response_id="old-response",
                microphone_pcm=b"\x33\x00" * 480,
            )))
        await asyncio.sleep(0)
        await bridge._enqueue_user_audio(utterance_id="queued-old", microphone_pcm=b"\x33\x00" * 480)
        discarded = bridge.invalidate_unfinalized_audio()
        assert "queued-old" in discarded
        await bridge._enqueue_user_audio(utterance_id="fresh", microphone_pcm=b"\x33\x00" * 480)
        await bridge._enqueue_user_audio(utterance_id="queued-fresh", microphone_pcm=b"\x33\x00" * 480)
        await asyncio.sleep(0)
        assert started == ["old", "fresh"]
        releases["old"].set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert started == ["old", "fresh"]
        releases["fresh"].set()
        async with asyncio.timeout(1):
            while "queued-fresh" not in releases:
                await asyncio.sleep(0)
        assert started == ["old", "fresh", "queued-fresh"]
        releases["queued-fresh"].set()
        await asyncio.gather(*tasks)

    asyncio.run(run())

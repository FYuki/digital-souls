"""capture/STTの単体試験用。VAD判定は代入し、正式境界の検出は実VAD module試験で検証する。"""

from __future__ import annotations

from uuid import uuid4
from unittest.mock import AsyncMock

from app.voice_input.detector import Detection
from app.voice_input.session import InputGrant, SpeechBoundary


def begin_capture(bridge, utterance_id: str, response_id: str | None = None) -> None:
    """BEが応答との関連を決めた後のcapture入口を呼ぶ。FEからの通知ではない。"""
    event = {"utterance_id": utterance_id}
    if response_id is not None:
        event["response_id"] = response_id
    bridge._begin_capture(event)


async def finish_capture(bridge, utterance_id: str) -> None:
    """PCMで終了を検出した後のcapture処理。通信と欠落確認はこの単体試験の対象外。"""
    if not hasattr(bridge._session, "session_id"):
        bridge._session.session_id = str(uuid4())
    if bridge._publish_audio_event is None:
        bridge._publish_audio_event = AsyncMock()
    if bridge._verify_audio_integrity is None:
        bridge._verify_audio_integrity = AsyncMock()
    end_sample = bridge._microphone_received_bytes // 2
    boundary = SpeechBoundary(
        utterance_id,
        InputGrant("TR_capture_fixture", 1, str(uuid4()), 1),
        Detection("ended", 0, end_sample, end_sample),
    )
    await bridge._audio_stopped(boundary)


def capture_harness():
    """推論・配信を行わず、captureの所有権とSTT直列queueを検証する。"""
    import asyncio
    from types import SimpleNamespace
    from app.livekit_transport.production import _ConversationCoreBridge

    requests, tasks = [], set()

    def schedule(operation):
        task = asyncio.create_task(operation)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    def transcribe(**request):
        requests.append(request)
        return schedule(asyncio.sleep(0))

    core = SimpleNamespace(
        session_id=str(uuid4()), accepting_input=True,
        start_transcription=transcribe, discard_utterance=AsyncMock(),
    )
    return _ConversationCoreBridge(core, schedule), requests, tasks

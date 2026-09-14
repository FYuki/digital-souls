"""BE実VAD→既存capture/STTの接続。STT自体の実接続受入は別suiteで行う。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4
import wave

import numpy as np
import pytest

from app.livekit_transport.delivery import TerminalProtocolError, decode_core_event
from app.livekit_transport.production import _ConversationCoreBridge
from app.voice_input.pipeline import AudioInputFault


class Core:
    def __init__(self):
        self.session_id = str(uuid4())
        self.active_response = None
        self.accepting_input = True
        self.transcriptions = []
        self.discards = []

    def start_transcription(self, **request):
        self.transcriptions.append(request)

        async def complete():
            return None

        return asyncio.create_task(complete())

    async def discard_utterance(self, **request):
        self.discards.append(request)


def pcm_fixture() -> bytes:
    path = (
        Path(__file__).resolve().parents[3] / "frontend/playwright/fixtures/speech.wav"
    )
    with wave.open(str(path)) as audio:
        data = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        rate = audio.getframerate()
    samples = np.interp(
        np.arange(0, len(data), rate / 16000), np.arange(len(data)), data
    ).astype("<i2")
    return np.concatenate(
        (np.zeros(16000, dtype="<i2"), samples, np.zeros(32000, dtype="<i2"))
    ).tobytes()


@pytest.mark.parametrize(
    "authorized,integrity,expected",
    [(True, True, 1), (False, True, 0), (True, False, 0)],
)
def test_real_vad_flows_through_bridge_only_after_track_and_integrity_checks(
    authorized, integrity, expected
) -> None:
    async def scenario() -> None:
        core, tasks, events = Core(), [], []

        async def publish(event):
            decode_core_event(json.dumps(event).encode())
            events.append(event)

        async def authorize(track):
            return authorized and track == "TR_microphone"

        async def verify(track, start, end):
            assert track == "TR_microphone" and 0 <= start <= end
            if not integrity:
                raise AudioInputFault("audio_integrity_unavailable")

        bridge = _ConversationCoreBridge(
            core,
            lambda operation: tasks.append(asyncio.create_task(operation)),
            publish_audio_event=publish,
            authorize_microphone=authorize,
            verify_audio_integrity=verify,
            user_participant_id=str(uuid4()),
        )
        try:
            await bridge.prepare_audio()
            bridge.notify(
                json.dumps(
                    {
                        "type": "audio_input_open_requested",
                        "event_id": str(uuid4()),
                        "track_sid": "TR_microphone",
                        "input_revision": 1,
                    }
                ).encode()
            )
            await asyncio.gather(*tasks)
            if not authorized:
                assert [event["type"] for event in events] == ["audio_input_rejected"]
            else:
                assert [event["type"] for event in events] == ["audio_input_opened"]
            pcm = pcm_fixture()
            for offset in range(0, len(pcm), 320):
                await bridge.receive_microphone_frame(
                    pcm[offset : offset + 320],
                    start_sample=offset // 2,
                    track_sid="TR_microphone",
                )
            await asyncio.gather(*tasks)
            assert len(core.transcriptions) == expected
            if authorized:
                speech = [
                    event
                    for event in events
                    if event["type"] in {"speech_started", "speech_stopped"}
                ]
                assert [event["type"] for event in speech] == [
                    "speech_started",
                    "speech_stopped",
                ]
                assert speech[0]["utterance_id"] == speech[1]["utterance_id"]
                assert all(
                    event["clock_domain"] == "server_monotonic" for event in speech
                )
            if authorized and not integrity:
                assert core.discards[0]["reason"] == "audio_integrity_unavailable"
                assert any(
                    event.get("error_code") == "audio_input_repeat_required"
                    for event in events
                )
            if expected:
                assert len(core.transcriptions[0]["audio"]) > 0
                assert (
                    core.transcriptions[0]["utterance_id"] == speech[0]["utterance_id"]
                )
        finally:
            await bridge.close_audio()
            await asyncio.gather(*tasks)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["speech_started", "speech_stopped"])
def test_client_cannot_supply_formal_speech_boundary(kind) -> None:
    bridge = _ConversationCoreBridge(Core(), lambda operation: operation.close())
    with pytest.raises(TerminalProtocolError, match="owned by Backend"):
        bridge.notify(json.dumps({"type": kind}).encode())

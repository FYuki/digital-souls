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


@pytest.mark.parametrize("first_integrity", [True, False])
def test_finished_capture_stays_frozen_while_integrity_waits_and_input_reopens(first_integrity):
    """実VADで終了した発話をfocusで保持し、新SIDのPCMを混入させない。"""
    async def scenario():
        core, tasks, events = Core(), [], []
        integrity_entered, integrity_release = asyncio.Event(), asyncio.Event()

        async def publish(event):
            decode_core_event(json.dumps(event).encode())
            events.append(event)

        async def authorize(track):
            return track in {"TR_first", "TR_next"}

        async def verify(track, start, end):
            if track == "TR_first":
                integrity_entered.set()
                await integrity_release.wait()
                if not first_integrity:
                    raise AudioInputFault("audio_integrity_unavailable")

        bridge = _ConversationCoreBridge(
            core, lambda operation: tasks.append(asyncio.create_task(operation)),
            publish_audio_event=publish, authorize_microphone=authorize,
            verify_audio_integrity=verify, user_participant_id=str(uuid4()),
        )

        async def open_input(track, revision):
            bridge.notify(json.dumps({
                "type": "audio_input_open_requested", "event_id": str(uuid4()),
                "track_sid": track, "input_revision": revision,
            }).encode())
            await asyncio.gather(*tasks)
            assert events[-1]["type"] == "audio_input_opened"

        async def feed(track):
            pcm = pcm_fixture()
            for offset in range(0, len(pcm), 320):
                await bridge.receive_microphone_frame(
                    pcm[offset:offset + 320], start_sample=offset // 2, track_sid=track,
                )

        feeding = None
        try:
            await bridge.prepare_audio()
            await open_input("TR_first", 1)
            feeding = asyncio.create_task(feed("TR_first"))
            await asyncio.wait_for(integrity_entered.wait(), 5)
            first_capture = bridge._user_audio_captures[0]
            first_id, first_pcm = first_capture.utterance_id, bytes(first_capture.pcm)
            assert first_capture.finalized and not core.transcriptions
            for revision, suppressed in [(2, True), (3, False)]:
                bridge.notify(json.dumps({
                    "type": "audio_input_suppression_changed", "input_revision": revision,
                    "suppressed": suppressed, "reason": "text_focus",
                }).encode())
            await open_input("TR_next", 4)
            await feed("TR_next")
            assert bytes(first_capture.pcm) == first_pcm
            assert not core.discards
            assert not core.transcriptions
            integrity_release.set()
            await asyncio.wait_for(feeding, 5)
            await asyncio.gather(*tasks)
            async def transcriptions_started():
                while len(core.transcriptions) < (2 if first_integrity else 1):
                    await asyncio.sleep(0)
            await asyncio.wait_for(transcriptions_started(), 1)
            starts = [event for event in events if event["type"] == "speech_started"]
            assert len(starts) == 2
            assert len(core.transcriptions) == (2 if first_integrity else 1)
            assert starts[0]["utterance_id"] == first_id
            assert starts[0]["utterance_id"] != starts[1]["utterance_id"]
            assert [request["utterance_id"] for request in core.transcriptions] == [
                event["utterance_id"] for event in (starts if first_integrity else starts[1:])
            ]
            if not first_integrity:
                assert core.discards == [{"utterance_id": first_id, "reason": "audio_integrity_unavailable"}]
        finally:
            integrity_release.set()
            if feeding is not None:
                await asyncio.gather(feeding, return_exceptions=True)
            await bridge.close_audio()
            await asyncio.gather(*tasks)

    asyncio.run(scenario())


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("failure", ["inference", "reset", "track_closed"])
def test_runtime_fault_is_recoverable_and_notifies_the_affected_input(active, failure):
    async def scenario():
        core, tasks, events = Core(), [], []
        async def publish(event):
            decode_core_event(json.dumps(event).encode())
            events.append(event)
        async def authorize(track):
            return True
        async def verify(track, start, end):
            return None
        bridge = _ConversationCoreBridge(
            core, lambda operation: tasks.append(asyncio.create_task(operation)),
            publish_audio_event=publish, authorize_microphone=authorize,
            verify_audio_integrity=verify, user_participant_id=str(uuid4()),
        )
        async def open_input(track, revision):
            await bridge._open_audio({
                "event_id": str(uuid4()), "track_sid": track, "input_revision": revision,
            })
        try:
            await bridge.prepare_audio()
            await open_input("TR_first", 1)
            source = bridge._voice_input
            pipeline = source._worker._pipeline
            grant = source.grant
            pcm = pcm_fixture()
            offset = 0
            if active:
                for offset in range(0, len(pcm), 320):
                    await bridge.receive_microphone_frame(
                        pcm[offset:offset + 320], start_sample=offset // 2,
                        track_sid="TR_first",
                    )
                    if source._active_utterance is not None:
                        offset += 320
                        break
                assert source._active_utterance is not None
            utterance_id = source._active_utterance
            original_feed, original_reset = pipeline.feed, pipeline.reset
            def fail(*args, **kwargs):
                raise RuntimeError("injected native failure")
            try:
                if failure == "track_closed":
                    bridge.close_microphone_track("TR_first")
                    # 二重終了は同じ入力に重ねて通知しない。
                    bridge.close_microphone_track("TR_first")
                else:
                    pipeline.feed = fail
                    if failure == "reset":
                        pipeline.reset = fail
                    await bridge.receive_microphone_frame(
                        pcm[offset:offset + 3072], start_sample=offset // 2,
                        track_sid="TR_first",
                    )
            finally:
                pipeline.feed, pipeline.reset = original_feed, original_reset
            await asyncio.gather(*tasks)
            assert not core.transcriptions
            if active:
                assert core.discards == [{
                    "utterance_id": utterance_id,
                    "reason": "audio_gap" if failure == "track_closed" else "vad_unavailable",
                }]
            else:
                assert not core.discards
            errors = [event for event in events if event["type"] == "error"]
            if failure == "inference":
                assert source.grant is grant
                assert [event["error_code"] for event in errors] == ["audio_input_repeat_required"]
            else:
                assert source.grant is None
                unavailable = [event for event in errors if event["error_code"] == "audio_input_unavailable"]
                assert len(unavailable) == 1
                assert unavailable[0]["user_state"] == "muted"
                assert unavailable[0]["track_sid"] == grant.track_sid
                assert unavailable[0]["input_generation"] == grant.input_generation
                assert unavailable[0]["input_revision"] == grant.input_revision
            await open_input("TR_next", 2)
            for offset in range(0, len(pcm), 320):
                await bridge.receive_microphone_frame(
                    pcm[offset:offset + 320], start_sample=offset // 2,
                    track_sid="TR_next",
                )
            await asyncio.gather(*tasks)
            assert len(core.transcriptions) == 1
            # 旧readerの遅着終了は再開した入力を停止しない。
            bridge.close_microphone_track("TR_first")
            assert source.grant.track_sid == "TR_next"
        finally:
            await bridge.close_audio()
            await asyncio.gather(*tasks)
    asyncio.run(scenario())


def test_reset_failure_during_open_publishes_rejection():
    async def scenario():
        core, tasks, events = Core(), [], []
        async def publish(event):
            decode_core_event(json.dumps(event).encode())
            events.append(event)
        async def authorize(track):
            return True
        bridge = _ConversationCoreBridge(
            core, lambda operation: tasks.append(asyncio.create_task(operation)),
            publish_audio_event=publish, authorize_microphone=authorize,
            user_participant_id=str(uuid4()),
        )
        try:
            await bridge.prepare_audio()
            pipeline = bridge._voice_input._worker._pipeline
            original = pipeline.reset
            def fail(*args, **kwargs):
                raise RuntimeError("injected reset failure")
            pipeline.reset = fail
            request = str(uuid4())
            try:
                await bridge._open_audio({
                    "event_id": request, "track_sid": "TR_first", "input_revision": 1,
                })
            finally:
                pipeline.reset = original
            assert len(events) == 1
            assert events[0]["type"] == "audio_input_rejected"
            assert events[0]["request_event_id"] == request
            assert events[0]["reason"] == "vad_unavailable"
        finally:
            await bridge.close_audio()
            await asyncio.gather(*tasks)
    asyncio.run(scenario())

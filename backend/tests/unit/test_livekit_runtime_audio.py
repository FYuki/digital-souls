from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import Awaitable
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import AsyncMock, Mock

import pytest

from app.conversation_core import CoreEvent
from app.livekit_transport import (
    core_delivery,
    core_factory,
    microphone_bridge,
    microphone_reader,
    production_sdk,
    session_runtime,
)
from app.livekit_transport.bootstrap import BootstrapTimeoutError

from tests.conversation_core_test_support import make_pcm16_wav
from tests.livekit_session_test_support import runtime_shell, session_owner
from tests.voice_capture_test_support import begin_capture, finish_capture, capture_harness

from tests.livekit_runtime_audio_test_support import (
    PCM_SAMPLE_RATE,
    PCM_CHANNELS,
    STT_TURN_PREVIEW_PCM_BYTES,
    STT_MICROPHONE_PREROLL_BYTES,
    STT_MAX_OPEN_CAPTURES,
    _runtime_module,
    RecordingObservationPort,
    NoopCoreSession,
    NoopCoreSessionFactory,
    _drain_asyncio_tasks,
)

def test_microphone_observation_records_metadata_without_audio_bytes() -> None:
    module = _runtime_module("metadata-only microphone observation")
    port = RecordingObservationPort()
    observer = module.MicrophoneTrackObserver(
        observation_port=port,
        observation_interval_ms=10,
    )

    observer.receive_frame(
        pcm=b"private-audio-sentinel",
        sample_count=480,
        received_at_ms=1_000,
    )
    observer.receive_frame(
        pcm=b"private-audio-sentinel-2",
        sample_count=480,
        received_at_ms=1_010,
    )

    assert port.records == [
        {
            "frame_count": 2,
            "sample_count": 960,
            "elapsed_ms": 10,
            "missing_frames": 0,
        }
    ]
    assert all(
        not isinstance(value, bytes)
        for record in port.records
        for value in record.values()
    )


def test_microphone_observation_is_throttled_to_one_per_second() -> None:
    module = _runtime_module("throttled microphone observation")
    port = RecordingObservationPort()
    observer = module.MicrophoneTrackObserver(observation_port=port)

    for frame_index in range(101):
        observer.receive_frame(
            pcm=b"audio-not-recorded",
            sample_count=480,
            received_at_ms=1_000 + frame_index * 10,
        )

    assert port.records == [
        {
            "frame_count": 101,
            "sample_count": 48_480,
            "elapsed_ms": 1_000,
            "missing_frames": 0,
        }
    ]


def test_stream_boundary_whitespace_never_reaches_voicevox_or_livekit_audio(
    monkeypatch,
) -> None:
    session_id = "20000000-0000-4000-8000-000000000010"
    events: list[tuple[str, object]] = []
    response_ids: list[str] = []
    trace_events: list[object] = []

    class AudioFrame:
        def __init__(
            self,
            data: bytes,
            sample_rate: int,
            channels: int,
            samples_per_channel: int,
        ) -> None:
            self.data = data
            self.sample_rate = sample_rate
            self.channels = channels
            self.samples_per_channel = samples_per_channel

    rtc = SimpleNamespace(AudioFrame=AudioFrame)
    monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(rtc=rtc))
    monkeypatch.setitem(sys.modules, "livekit.rtc", rtc)

    class RecordingCoordinator:
        def begin_response(self, *, response_id: str) -> None:
            response_ids.append(response_id)

        async def send_logical_audio_segment(self, **metadata: object) -> None:
            events.append(("logical_metadata", metadata))

        async def send_core(self, payload: bytes) -> None:
            events.append(("metadata", json.loads(payload)))

    class RtcAudioSource:
        async def capture_frame(self, frame: AudioFrame) -> None:
            events.append(("frame", frame))

        def clear_queue(self) -> None:
            events.append(("clear", None))

    class Synthesizer:
        def synthesize(self, text: str, speaker_id: int) -> bytes:
            assert text == "光織の応答"
            assert speaker_id == 7
            return make_pcm16_wav(
                pcm=b"\x00\x00" * 4,
                sample_rate=24_000,
                channels=1,
            )

    class Transcriber:
        def transcribe(self, audio: bytes) -> str:
            raise AssertionError(f"STT must not run: {audio!r}")

    class StartedTurn:
        content_skipped = False

    class HistorySession:
        def start_turn(self, user_content: str) -> StartedTurn:
            assert user_content == "利用者の発話"
            return StartedTurn()

        def complete_turn(
            self, started_turn: object, assistant_content: str
        ) -> None:
            assert isinstance(started_turn, StartedTurn)
            assert assistant_content == "光織の応答"

        def interrupt_turn(self, *_request: object) -> None:
            raise AssertionError("completed response must not be interrupted")

        def fail_turn(self, _started_turn: object) -> None:
            raise AssertionError("completed response must not fail")

    class HistoryService:
        def open_session(
            self, character_id: str, conversation_id: UUID
        ) -> HistorySession:
            assert character_id == "miori"
            assert conversation_id == UUID(
                "60000000-0000-4000-8000-000000000010"
            )
            return HistorySession()

    def load_tts_config(character_id: str) -> SimpleNamespace:
        assert character_id == "miori"
        return SimpleNamespace(speaker_id=7)

    async def generate_reply_stream(
        character_id: str,
        history_session: object,
        transcript: str,
    ):
        assert character_id == "miori"
        assert isinstance(history_session, HistorySession)
        assert transcript == "利用者の発話"
        yield "\n"
        yield "光織"
        yield "の応答"
        yield "\n"

    monkeypatch.setattr(core_factory, "load_tts_config", load_tts_config)
    coordinator = RecordingCoordinator()
    delivery = core_delivery._ConversationCoreDelivery(
        coordinator=coordinator,
        audio_source=core_delivery._LiveKitPcmAudioSource(RtcAudioSource()),
        character_participant_id="40000000-0000-4000-8000-000000000010",
        character_id="miori",
    )
    factory = core_factory.ProductionConversationCoreSessionFactory(
        transcriber=Transcriber(),
        synthesizer=Synthesizer(),
        history_service=HistoryService(),
        generate_reply_stream=generate_reply_stream,
        measurement_kind="dogfood",
        trace_record=trace_events.append,
        measurement_clock_ns=iter(range(1_000, 2_000)).__next__,
    )

    async def exercise() -> None:
        session = factory.create(
            session_id=session_id,
            character_id="miori",
            conversation_id=UUID("60000000-0000-4000-8000-000000000010"),
            delivery=delivery,
        )
        response = await session.finalize_utterance(
            utterance_id="30000000-0000-4000-8000-000000000010",
            transcript="利用者の発話",
            should_response=True,
        )

        async def wait_for_completion() -> None:
            while session.active_response is not None:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_completion(), timeout=0.5)
        assert response is not None
        assert session.response(response.response_id).state.value == "completed"
        await session.end()

    asyncio.run(exercise())

    audio_metadata_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "metadata"
        and event[1]["type"] == "response_audio_segment"
    )
    metadata = events[audio_metadata_index][1]
    frame = events[audio_metadata_index + 1][1]
    assert events[audio_metadata_index - 1] == (
        "logical_metadata",
        {
            "response_id": metadata["response_id"],
            "audio_sequence": 0,
            "pcm_sample_count": 7,
        },
    )
    assert response_ids == [metadata["response_id"]]
    assert metadata["audio_sequence"] == 1
    assert metadata["text_range"] == {"start": 0, "end": 5}
    assert "audio" not in metadata
    assert isinstance(frame, AudioFrame)
    assert frame.data == b"\x00\x00" * 7
    assert not frame.data.startswith(b"RIFF")
    assert frame.sample_rate == 48_000
    assert frame.channels == 1
    assert frame.samples_per_channel == len(frame.data) // 2
    assert {
        event.name for event in trace_events  # type: ignore[attr-defined]
    } >= {
        "response_decision",
        "llm_started",
        "first_text_delta",
        "tts_started",
        "tts_completed",
        "first_audio_generated",
        "first_audio_out",
    }
    assert {
        (event.session_id, event.utterance_id, event.response_id)  # type: ignore[attr-defined]
        for event in trace_events
    } == {
        (
            session_id,
            "30000000-0000-4000-8000-000000000010",
            metadata["response_id"],
        )
    }
    assert {
        event.character_id for event in trace_events  # type: ignore[attr-defined]
    } == {"miori"}
    trace_by_name = {
        event.name: event for event in trace_events  # type: ignore[attr-defined]
    }
    assert (
        trace_by_name["first_text_delta"].timestamp
        < trace_by_name["tts_started"].timestamp
        <= trace_by_name["tts_completed"].timestamp
    )


def test_cancel_clears_character_audio_queue_and_next_response_can_publish(
    monkeypatch,
) -> None:
    operations: list[tuple[str, object]] = []

    class AudioFrame:
        def __init__(
            self,
            data: bytes,
            sample_rate: int,
            channels: int,
            samples_per_channel: int,
        ) -> None:
            del sample_rate, channels, samples_per_channel
            self.data = data

    rtc = SimpleNamespace(AudioFrame=AudioFrame)
    monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(rtc=rtc))
    monkeypatch.setitem(sys.modules, "livekit.rtc", rtc)

    class Coordinator:
        def begin_response(self, *, response_id: str) -> None:
            del response_id

        async def send_logical_audio_segment(self, **metadata: object) -> None:
            operations.append(("logical", metadata))

        async def send_core(self, payload: bytes) -> None:
            operations.append(("core", json.loads(payload)))

    class RtcAudioSource:
        async def capture_frame(self, frame: AudioFrame) -> None:
            operations.append(("frame", frame.data))

        def clear_queue(self) -> None:
            operations.append(("clear", None))

    delivery = core_delivery._ConversationCoreDelivery(
        coordinator=Coordinator(),
        audio_source=core_delivery._LiveKitPcmAudioSource(RtcAudioSource()),
        character_participant_id="40000000-0000-4000-8000-000000000010",
        character_id="miori",
    )

    async def exercise() -> None:
        await delivery.publish(CoreEvent(
            type="response_audio_segment",
            session_id="20000000-0000-4000-8000-000000000010",
            response_id="50000000-0000-4000-8000-000000000010",
            audio_sequence=1,
            audio=b"\x01\x00" * 4,
            text_range=(0, 2),
        ))
        await delivery.publish(CoreEvent(
            type="response_cancelled",
            session_id="20000000-0000-4000-8000-000000000010",
            response_id="50000000-0000-4000-8000-000000000010",
            reason="user_request",
        ))
        await delivery.publish(CoreEvent(
            type="response_audio_segment",
            session_id="20000000-0000-4000-8000-000000000010",
            response_id="50000000-0000-4000-8000-000000000020",
            audio_sequence=1,
            audio=b"\x02\x00" * 3,
            text_range=(0, 2),
        ))

    asyncio.run(exercise())

    assert [name for name, _value in operations] == [
        "logical", "core", "frame", "core", "clear",
        "core", "logical", "core", "frame", "core",
    ]
    assert ("frame", b"\x02\x00" * 3) in operations


@pytest.mark.parametrize("source", ["speech", "text"])
def test_response_started_character_speaker_passes_schema_validation(source) -> None:
    coordinator_module = importlib.import_module("app.livekit_transport.coordinator")
    session_id = "20000000-0000-4000-8000-000000000010"
    participant_id = "40000000-0000-4000-8000-000000000010"
    published: list[bytes] = []

    class CorePort:
        def notify(self, payload: bytes) -> None:
            del payload

    class AudioSource:
        async def begin_response(self, response_id: str) -> None:
            pass

        async def publish(self, pcm: bytes, *, response_id: str) -> None:
            raise AssertionError(f"response_started must not publish audio: {pcm!r}")

    async def exercise() -> None:
        async def publish_data(payload: bytes, topic: str) -> None:
            assert topic == coordinator_module.APPLICATION_TOPIC
            published.append(payload)

        async def cleanup(owned_session_id: str) -> None:
            assert owned_session_id == session_id

        async def generation_ready() -> None:
            return None

        coordinator = coordinator_module.ProductionSessionCoordinator(
            session_id=session_id,
            user_identity=f"user-{session_id}",
            core_participant_id="41000000-0000-4000-8000-000000000010",
            reconnect_grace_ms=60_000,
            dependencies=coordinator_module.SessionCoordinatorDependencies(
                publish_data=publish_data,
                cleanup=cleanup,
                generation_ready=generation_ready,
            ),
            core_port=CorePort(),
        )
        coordinator.participant_connected(
            identity=f"user-{session_id}",
            participant_sid="PA_user",
            room_sid="RM_room",
        )
        delivery = core_delivery._ConversationCoreDelivery(
            coordinator=coordinator,
            audio_source=AudioSource(),
            character_participant_id=participant_id,
            character_id="miori",
        )
        from app.conversation_core import InputSource
        from app.livekit_transport.measurement import LiveKitMeasurementSession
        delivery.attach_measurement(LiveKitMeasurementSession(
            session_id=session_id, character_id="miori", measurement_kind="automated_test",
            record=None, clock_ns=lambda: 1,
        ))
        event = CoreEvent(
            type="response_started",
            session_id=session_id,
            response_id="50000000-0000-4000-8000-000000000010",
            history_turn_id="60000000-0000-4000-8000-000000000010",
            source_utterance_ids=(
                "30000000-0000-4000-8000-000000000010",
            ) if source == "speech" else (),
            source_inputs=(InputSource("30000000-0000-4000-8000-000000000010", source),),
        )

        await delivery.publish(event)
        payload = json.loads(published[0])
        coordinator.acknowledge(str(payload["event_id"]), "character_to_user")
        await coordinator.cleanup("test_complete")

    asyncio.run(exercise())

    payload = json.loads(published[0])
    assert payload["history_turn_id"] == "60000000-0000-4000-8000-000000000010"
    assert payload["speaker"] == {
        "participant_id": participant_id,
        "role": "character",
        "character_id": "miori",
    }


def test_production_core_bridge_routes_microphone_and_control_to_one_session() -> None:
    delivery = importlib.import_module("app.livekit_transport.delivery")
    calls: list[tuple[str, object]] = []
    tasks: set[asyncio.Task[None]] = set()

    class RecordingCoreSession:
        def start_transcription(self, **request: object) -> asyncio.Task[None]:
            async def record() -> None:
                calls.append(("transcription", request))

            return asyncio.create_task(record())

        async def cancel_response(self, **request: object) -> None:
            calls.append(("cancel", request))

        async def confirm_playback(self, **request: object) -> None:
            calls.append(("playback", request))

        async def disconnect(self) -> None:
            calls.append(("disconnect", None))

        async def reconnect(self) -> None:
            calls.append(("reconnect", None))

        async def end(self) -> None:
            calls.append(("end", None))

    def schedule(operation) -> None:
        task = asyncio.create_task(operation)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    session = RecordingCoreSession()
    bridge = microphone_bridge._ConversationCoreBridge(
        session, schedule
    )

    async def exercise() -> None:
        common = {
            "protocol_version": "2.0",
            "event_id": "10000000-0000-4000-8000-000000000010",
            "session_id": "20000000-0000-4000-8000-000000000010",
            "monotonic_timestamp_ms": 1_000,
        }
        user_speaker = {
            "participant_id": "40000000-0000-4000-8000-000000000010",
            "role": "user",
        }
        speech_started = json.dumps(
            {
                **common,
                "type": "speech_started",
                "input_generation": 1, "track_sid": "TR_fixture",
                "start_sample": 0, "active_end_sample": 4, "detected_sample": 4,
                "sample_rate": 16000, "clock_domain": "server_monotonic",
                "speaker": user_speaker,
                "utterance_id": "30000000-0000-4000-8000-000000000010",
            }
        ).encode()
        delivery.decode_core_event(speech_started)
        bridge._begin_capture(json.loads(speech_started))
        bridge.receive_microphone(b"live-pcm")
        speech_stopped = json.dumps(
            {
                **common,
                "event_id": "10000000-0000-4000-8000-000000000011",
                "type": "speech_stopped",
                "input_generation": 1, "track_sid": "TR_fixture",
                "start_sample": 0, "active_end_sample": 4, "detected_sample": 4,
                "sample_rate": 16000, "clock_domain": "server_monotonic",
                "speaker": user_speaker,
                "utterance_id": "30000000-0000-4000-8000-000000000010",
            }
        ).encode()
        delivery.decode_core_event(speech_stopped)
        await finish_capture(bridge, json.loads(speech_stopped)["utterance_id"])
        bridge.notify(
            json.dumps(
                {
                    **common,
                    "type": "response_cancel_requested",
                    "response_id": "50000000-0000-4000-8000-000000000010",
                    "reason": "barge_in",
                }
            ).encode()
        )
        bridge.notify(
            json.dumps(
                {
                    **common,
                    "type": "playback_completed",
                    "response_id": "50000000-0000-4000-8000-000000000010",
                    "last_played_audio_sequence": 1,
                }
            ).encode()
        )
        bridge.notify(json.dumps({**common, "type": "session_disconnected"}).encode())
        bridge.notify(json.dumps({**common, "type": "session_reconnected"}).encode())
        await _drain_asyncio_tasks(tasks)
        await bridge.end()

    asyncio.run(exercise())

    assert next(call for call in calls if call[0] == "transcription") == (
        "transcription",
        {
            "utterance_id": "30000000-0000-4000-8000-000000000010",
            "audio": b"live-pcm",
            "should_response": True,
        },
    )
    assert sorted(name for name, _request in calls) == sorted([
        "transcription",
        "cancel",
        "playback",
        "disconnect",
        "reconnect",
        "end",
    ])


def test_production_core_bridge_keeps_preroll_before_speech_started() -> None:
    requests: list[dict[str, object]] = []
    tasks: set[asyncio.Task[None]] = set()

    class RecordingCoreSession:
        accepting_input = True

        def start_transcription(self, **request: object) -> asyncio.Task[None]:
            async def record() -> None:
                requests.append(request)

            return asyncio.create_task(record())

        async def end(self) -> None:
            return None

    def schedule(operation: Awaitable[None]) -> None:
        task = asyncio.create_task(operation)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    bridge = microphone_bridge._ConversationCoreBridge(
        RecordingCoreSession(), schedule
    )

    async def exercise() -> None:
        bridge.receive_microphone(b"pre-")
        common = {
            "speaker": {"role": "user"},
            "utterance_id": "30000000-0000-4000-8000-000000000020",
        }
        bridge._begin_capture({**common, "type": "speech_started"})
        bridge.receive_microphone(b"live")
        await finish_capture(bridge, {**common, "type": "speech_stopped"}["utterance_id"])
        await _drain_asyncio_tasks(tasks)

    asyncio.run(exercise())

    assert requests[0]["audio"] == b"pre-live"


def test_production_core_bridge_keeps_full_vad_confirmation_window() -> None:
    requests: list[dict[str, object]] = []
    tasks: set[asyncio.Task[None]] = set()

    class RecordingCoreSession:
        accepting_input = True

        def start_transcription(self, **request: object) -> asyncio.Task[None]:
            async def record() -> None:
                requests.append(request)

            return asyncio.create_task(record())

    def schedule(operation: Awaitable[None]) -> None:
        task = asyncio.create_task(operation)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    bridge = microphone_bridge._ConversationCoreBridge(
        RecordingCoreSession(), schedule
    )
    preroll = bytes(
        index % 251
        for index in range(STT_MICROPHONE_PREROLL_BYTES + 257)
    )

    async def exercise() -> None:
        bridge.receive_microphone(preroll)
        common = {
            "speaker": {"role": "user"},
            "utterance_id": "30000000-0000-4000-8000-000000000022",
        }
        bridge._begin_capture({**common, "type": "speech_started"})
        bridge.receive_microphone(b"live")
        await finish_capture(bridge, {**common, "type": "speech_stopped"}["utterance_id"])
        await _drain_asyncio_tasks(tasks)

    asyncio.run(exercise())

    # 実fixtureの最大確認遅れ1,440msと到達差の余裕を保持し、2秒を上限とする。
    retained_ms = STT_MICROPHONE_PREROLL_BYTES * 1000 / (16_000 * 2)
    assert 1600 <= retained_ms <= 2000
    assert requests[0]["audio"] == (
        preroll[-STT_MICROPHONE_PREROLL_BYTES :] + b"live"
    )


def test_production_core_bridge_previews_first_audio_before_speech_end() -> None:
    previews: list[dict[str, object]] = []
    tasks: set[asyncio.Task[None]] = set()

    class RecordingCoreSession:
        accepting_input = True

        async def preview_turn(self, **request: object) -> str:
            previews.append(request)
            return "take_turn"

    def schedule(operation: Awaitable[None]) -> None:
        task = asyncio.create_task(operation)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    bridge = microphone_bridge._ConversationCoreBridge(RecordingCoreSession(), schedule)

    async def exercise() -> None:
        bridge._begin_capture({
                    "type": "speech_started",
                    "speaker": {"role": "user"},
                    "utterance_id": "30000000-0000-4000-8000-000000000021",
                    "response_id": "50000000-0000-4000-8000-000000000021",
                })
        bridge.receive_microphone(b"x" * STT_TURN_PREVIEW_PCM_BYTES)
        await _drain_asyncio_tasks(tasks)

    asyncio.run(exercise())

    assert callable(previews[0].pop("input_is_current"))
    assert previews == [
        {
            "utterance_id": "30000000-0000-4000-8000-000000000021",
            "audio": b"x" * STT_TURN_PREVIEW_PCM_BYTES,
            "interrupted_response_id": "50000000-0000-4000-8000-000000000021",
        }
    ]


@pytest.mark.parametrize("prefix_samples", [0, 32_000])
def test_turn_preview_waits_for_audio_after_signal_onset(prefix_samples: int) -> None:
    """長い静音のpre-rollを800msの発話冒頭として数えない。"""
    previews: list[bytes] = []
    tasks: set[asyncio.Task[None]] = set()

    class Core:
        accepting_input = True

        async def preview_turn(self, **request: object) -> str:
            previews.append(request["audio"])
            return "take_turn"

    def schedule(operation: Awaitable[None]) -> None:
        task = asyncio.create_task(operation)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    bridge = microphone_bridge._ConversationCoreBridge(Core(), schedule)
    voice = b"\x00\x10" * (STT_TURN_PREVIEW_PCM_BYTES // 2)

    async def exercise() -> None:
        bridge.receive_microphone(bytes(prefix_samples * 2))
        bridge._begin_capture({"type": "speech_started", "speaker": {"role": "user"},
                                  "utterance_id": "preview-onset", "response_id": "old"})
        bridge.receive_microphone(voice[:6400])  # 実音声200ms。
        await _drain_asyncio_tasks(tasks)
        assert previews == []
        bridge.receive_microphone(voice[6400:])
        await _drain_asyncio_tasks(tasks)
        assert previews == [bytes(min(prefix_samples, 5120) * 2) + voice]
        bridge.receive_microphone(voice)
        await _drain_asyncio_tasks(tasks)
        assert len(previews) == 1

    asyncio.run(exercise())


def test_turn_preview_does_not_transcribe_silence_or_closed_input() -> None:
    scheduled: list[Awaitable[None]] = []

    class Core:
        accepting_input = True

    core = Core()
    bridge = microphone_bridge._ConversationCoreBridge(core, scheduled.append)
    bridge._begin_capture({"type": "speech_started", "speaker": {"role": "user"},
                              "utterance_id": "preview-silence", "response_id": "old"})
    bridge.receive_microphone(bytes(STT_TURN_PREVIEW_PCM_BYTES * 2))
    try:
        assert scheduled == []
        core.accepting_input = False
        bridge.receive_microphone(b"\x00\x10" * STT_TURN_PREVIEW_PCM_BYTES)
        assert scheduled == []
    finally:
        for operation in scheduled:
            operation.close()


def test_production_core_bridge_stops_server_audio_for_playback_stop() -> None:
    scheduled: list[Awaitable[None]] = []
    stopped: list[str] = []

    class RecordingCoreSession:
        async def confirm_playback(self, **_request: object) -> bool:
            return True

    bridge = microphone_bridge._ConversationCoreBridge(
        RecordingCoreSession(),
        scheduled.append,
        stop_audio=stopped.append,
        )
    bridge.notify(
        json.dumps(
            {
                "type": "playback_stopped",
                "response_id": "50000000-0000-4000-8000-000000000010",
                "reason": "barge_in",
                "last_played_audio_sequence": 1,
            }
        ).encode()
    )

    async def exercise() -> None:
        for operation in scheduled:
            await operation

    asyncio.run(exercise())

    assert stopped == ["50000000-0000-4000-8000-000000000010"]


def test_production_core_bridge_stops_audio_before_prefix_validation_failure() -> None:
    scheduled: list[Awaitable[None]] = []
    operations: list[str] = []

    class RejectingCoreSession:
        async def confirm_playback(self, **_request: object) -> bool:
            operations.append("confirm")
            raise ValueError("invalid playback prefix")

    bridge = microphone_bridge._ConversationCoreBridge(
        RejectingCoreSession(),
        scheduled.append,
        stop_audio=lambda _response_id: operations.append("stop"),
        )
    bridge.notify(
        json.dumps(
            {
                "type": "playback_stopped",
                "response_id": "50000000-0000-4000-8000-000000000010",
                "reason": "barge_in",
                "last_played_audio_sequence": 1,
            }
        ).encode()
    )

    async def exercise() -> None:
        with pytest.raises(ValueError, match="invalid playback prefix"):
            await scheduled[0]

    asyncio.run(exercise())

    assert operations == ["stop", "confirm"]


def test_production_core_bridge_bounds_waiting_stt_and_discards_overflow() -> None:
    tasks: set[asyncio.Task[None]] = set()
    started: list[str] = []
    discarded: list[tuple[str, str]] = []

    class RecordingCoreSession:
        accepting_input = True

        def start_transcription(self, **request: object) -> asyncio.Task[None]:
            async def record() -> None:
                started.append(str(request["utterance_id"]))

            task = asyncio.create_task(record())
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            return task

        async def discard_utterance(self, **request: str) -> bool:
            discarded.append((request["utterance_id"], request["reason"]))
            return True

    def schedule(operation: Awaitable[None]) -> None:
        task = asyncio.create_task(operation)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    bridge = microphone_bridge._ConversationCoreBridge(
        RecordingCoreSession(), schedule
    )

    async def exercise() -> None:
        # active 1件を確保した状態で、待機3件と超過1件を投入する。
        bridge._transcription_active = True
        for index in range(5):
            await bridge._enqueue_user_audio(
                utterance_id=f"utterance-{index}",
                microphone_pcm=b"pcm0",
            )
        bridge._transcription_active = False
        utterance_id, microphone_pcm, interrupted_response_id = (
            bridge._pending_transcriptions.popleft()
        )
        bridge._pending_transcription_bytes -= len(microphone_pcm)
        bridge._start_user_transcription(
            utterance_id, microphone_pcm, interrupted_response_id
        )
        for _ in range(20):
            if not tasks:
                break
            await asyncio.sleep(0)

    asyncio.run(exercise())

    assert started == ["utterance-0", "utterance-1", "utterance-2"]
    assert discarded == [
        ("utterance-3", "input_capacity_exceeded"),
        ("utterance-4", "input_capacity_exceeded"),
    ]


def test_production_core_bridge_does_not_transcribe_after_core_disconnect() -> None:
    tasks: set[asyncio.Task[None]] = set()

    class DisconnectedCoreSession:
        accepting_input = False

        def start_transcription(self, **_request: object) -> asyncio.Task[None]:
            raise AssertionError("切断後の発話をSTTへ渡してはならない")

    def schedule(operation) -> None:
        task = asyncio.create_task(operation)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    bridge = microphone_bridge._ConversationCoreBridge(
        DisconnectedCoreSession(), schedule
    )

    async def exercise() -> None:
        common = {
            "protocol_version": "1.1",
            "event_id": "10000000-0000-4000-8000-000000000020",
            "session_id": "20000000-0000-4000-8000-000000000020",
            "monotonic_timestamp_ms": 1_000,
            "speaker": {
                "participant_id": "40000000-0000-4000-8000-000000000020",
                "role": "user",
            },
            "utterance_id": "30000000-0000-4000-8000-000000000020",
        }
        bridge._begin_capture({**common, "type": "speech_started"})
        bridge.receive_microphone(b"stale-pcm!")
        await finish_capture(bridge, {
                    **common,
                    "event_id": "10000000-0000-4000-8000-000000000021",
                    "type": "speech_stopped",
                }["utterance_id"])
        await _drain_asyncio_tasks(tasks)

    asyncio.run(exercise())


def test_production_core_bridge_discards_malformed_control_events() -> None:
    calls: list[dict[str, object]] = []
    scheduled: list[Awaitable[None]] = []

    class RecordingCoreSession:
        async def cancel_response(self, **request: object) -> None:
            calls.append(request)

        async def confirm_playback(self, **request: object) -> None:
            calls.append(request)

    bridge = microphone_bridge._ConversationCoreBridge(
        RecordingCoreSession(), scheduled.append
    )

    async def exercise() -> None:
        for event in (
            {"type": "response_cancel_requested", "reason": "barge_in"},
            {
                "type": "response_cancel_requested",
                "response_id": "response-id",
                "reason": 1,
            },
            {
                "type": "playback_completed",
                "response_id": "response-id",
                "last_played_audio_sequence": True,
            },
        ):
            bridge.notify(json.dumps(event).encode())
        for operation in scheduled:
            await operation

    asyncio.run(exercise())

    assert calls == []


def test_production_core_bridge_keeps_pcm_owned_by_each_consecutive_utterance():
    async def exercise():
        bridge, requests, tasks = capture_harness()
        for name, pcm in [("first", b"pcm-one!"), ("second", b"pcm-two!")]:
            begin_capture(bridge, name)
            bridge.receive_microphone(pcm)
            await finish_capture(bridge, name)
        await _drain_asyncio_tasks(tasks)
        assert [(r["utterance_id"], r["audio"]) for r in requests] == [
            ("first", b"pcm-one!"), ("second", b"pcm-two!"),
        ]
        assert all(r["should_response"] is True for r in requests)
    asyncio.run(exercise())


def test_production_core_bridge_does_not_assign_current_pcm_to_old_empty_capture():
    async def exercise():
        bridge, requests, tasks = capture_harness()
        for name in ["empty-first", "empty-second"]:
            begin_capture(bridge, name)
            await finish_capture(bridge, name)
        begin_capture(bridge, "third")
        bridge.receive_microphone(b"pcm-three!")
        await finish_capture(bridge, "third")
        await _drain_asyncio_tasks(tasks)
        assert [(r["utterance_id"], r["audio"]) for r in requests] == [("third", b"pcm-three!")]
    asyncio.run(exercise())


def test_production_core_bridge_keeps_post_boundary_pcm_only_for_the_next_capture():
    async def exercise():
        bridge, requests, tasks = capture_harness()
        begin_capture(bridge, "first")
        bridge.receive_microphone(b"pcm-one!")
        await finish_capture(bridge, "first")
        bridge.receive_microphone(b"next-prefix!")
        await _drain_asyncio_tasks(tasks)
        assert [r["audio"] for r in requests] == [b"pcm-one!"]
        begin_capture(bridge, "second")
        bridge.receive_microphone(b"pcm-two!")
        await finish_capture(bridge, "second")
        await _drain_asyncio_tasks(tasks)
        assert [r["audio"] for r in requests] == [b"pcm-one!", b"next-prefix!pcm-two!"]
    asyncio.run(exercise())


def test_production_core_bridge_separates_overlapping_utterance_pcm():
    # captureの防御処理。実BE VADは単一trackで発話区間を直列に確定する。
    async def exercise():
        bridge, requests, tasks = capture_harness()
        begin_capture(bridge, "first")
        bridge.receive_microphone(b"pcm-one!")
        begin_capture(bridge, "second")
        bridge.receive_microphone(b"pcm-two!")
        await finish_capture(bridge, "first")
        await finish_capture(bridge, "second")
        await _drain_asyncio_tasks(tasks)
        assert [(r["utterance_id"], r["audio"]) for r in requests] == [
            ("first", b"pcm-one!"), ("second", b"pcm-two!"),
        ]
    asyncio.run(exercise())


def test_production_core_bridge_discards_oldest_unfinished_capture_at_limit() -> None:
    discarded: list[dict[str, object]] = []
    scheduled: list[Awaitable[None]] = []

    class RecordingCoreSession:
        async def discard_utterance(self, **request: object) -> None:
            discarded.append(request)

    bridge = microphone_bridge._ConversationCoreBridge(
        RecordingCoreSession(), scheduled.append
    )
    utterance_ids = [
        f"30000000-0000-4000-8000-{index:012d}"
        for index in range(STT_MAX_OPEN_CAPTURES + 1)
    ]

    async def exercise() -> None:
        for utterance_id in utterance_ids:
            bridge._begin_capture({
                        "type": "speech_started",
                        "utterance_id": utterance_id,
                        "speaker": {"role": "user"},
                    })
        for operation in scheduled:
            await operation

    asyncio.run(exercise())

    assert [capture.utterance_id for capture in bridge._user_audio_captures] == (
        utterance_ids[1:]
    )
    assert discarded == [
        {
            "utterance_id": utterance_ids[0],
            "reason": "input_capacity_exceeded",
        }
    ]


def test_production_core_bridge_empty_capture_does_not_block_later_audio():
    async def exercise():
        bridge, requests, tasks = capture_harness()
        begin_capture(bridge, "empty")
        begin_capture(bridge, "second")
        bridge.receive_microphone(b"pcm-two!")
        await finish_capture(bridge, "empty")
        await finish_capture(bridge, "second")
        await _drain_asyncio_tasks(tasks)
        assert [(r["utterance_id"], r["audio"]) for r in requests] == [("second", b"pcm-two!")]
    asyncio.run(exercise())


def test_production_core_bridge_does_not_attach_later_pcm_to_an_empty_ended_boundary():
    async def exercise():
        bridge, requests, tasks = capture_harness()
        begin_capture(bridge, "empty")
        await finish_capture(bridge, "empty")
        bridge.receive_microphone(b"late-pcm")
        await _drain_asyncio_tasks(tasks)
        assert requests == []
        begin_capture(bridge, "next")
        await finish_capture(bridge, "next")
        await _drain_asyncio_tasks(tasks)
        assert [(r["utterance_id"], r["audio"]) for r in requests] == [("next", b"late-pcm")]
    asyncio.run(exercise())


def test_production_core_bridge_rejects_prebind_client_boundaries():
    from app.livekit_transport.core_delivery import ProductionCoreEventInbox
    from app.livekit_transport.delivery import TerminalProtocolError
    async def exercise():
        bridge, requests, tasks = capture_harness()
        inbox = ProductionCoreEventInbox()
        session_id = bridge._session.session_id
        inbox.notify(json.dumps({
            "session_id": session_id, "event_id": "client-boundary",
            "type": "speech_stopped", "utterance_id": "forged", "transcript": "偽の本文",
        }).encode())
        with pytest.raises(TerminalProtocolError, match="owned by Backend"):
            inbox.bind(session_id, bridge.notify)
        bridge.receive_microphone(b"late-pcm")
        await _drain_asyncio_tasks(tasks)
        assert requests == []
        inbox.unbind(session_id)
    asyncio.run(exercise())


def test_production_core_bridge_does_not_use_client_text_without_pcm():
    from app.livekit_transport.delivery import TerminalProtocolError
    async def exercise():
        bridge, requests, tasks = capture_harness()
        for kind in ["speech_started", "speech_stopped"]:
            with pytest.raises(TerminalProtocolError, match="owned by Backend"):
                bridge.notify(json.dumps({
                    "type": kind, "utterance_id": "forged",
                    "transcript": "偽のclient本文", "should_response": True,
                }).encode())
        await _drain_asyncio_tasks(tasks)
        assert requests == []
        assert not bridge._user_audio_captures
    asyncio.run(exercise())


def test_production_core_event_inbox_delivers_each_session_in_order_once() -> None:
    inbox = core_delivery.ProductionCoreEventInbox()
    session_id = "20000000-0000-4000-8000-000000000010"
    other_session_id = "20000000-0000-4000-8000-000000000011"
    received: list[str] = []
    other_received: list[str] = []

    def payload(target_session_id: str, event_type: str) -> bytes:
        return json.dumps(
            {"session_id": target_session_id, "type": event_type}
        ).encode()

    inbox.notify(payload(session_id, "session_disconnected"))
    inbox.notify(payload(other_session_id, "session_disconnected"))
    inbox.notify(payload(session_id, "session_reconnected"))

    inbox.bind(
        session_id,
        lambda event: received.append(json.loads(event)["type"]),
    )
    inbox.notify(payload(session_id, "session_disconnected"))
    inbox.bind(
        other_session_id,
        lambda event: other_received.append(json.loads(event)["type"]),
    )

    assert received == [
        "session_disconnected",
        "session_reconnected",
        "session_disconnected",
    ]
    assert other_received == ["session_disconnected"]


def test_production_core_event_inbox_unbind_discards_session_state() -> None:
    inbox = core_delivery.ProductionCoreEventInbox()
    session_id = "20000000-0000-4000-8000-000000000010"
    received: list[str] = []

    def payload(event_type: str) -> bytes:
        return json.dumps({"session_id": session_id, "type": event_type}).encode()

    inbox.bind(
        session_id,
        lambda event: received.append(json.loads(event)["type"]),
    )
    inbox.notify(payload("session_disconnected"))
    inbox.unbind(session_id)
    inbox.notify(payload("session_reconnected"))
    inbox.unbind(session_id)

    inbox.bind(
        session_id,
        lambda event: received.append(json.loads(event)["type"]),
    )
    inbox.notify(payload("session_started"))

    assert received == ["session_disconnected", "session_started"]


def test_character_runtime_uses_microphone_grant_and_matching_publish_source(
    monkeypatch,
) -> None:
    token_requests: list[dict[str, object]] = []
    published_tracks: list[tuple[object, object]] = []

    class RecordingSigner:
        async def issue_token(self, request: dict[str, object]) -> str:
            token_requests.append(request)
            return "character-token"

    class LocalParticipant:
        async def publish_data(
            self, payload: bytes, *, reliable: bool, topic: str
        ) -> None:
            del payload, reliable, topic

        async def publish_track(self, track: object, options: object) -> None:
            published_tracks.append((track, options))

    class Room:
        sid = "RM_test"

        def __init__(self) -> None:
            self.local_participant = LocalParticipant()

        def on(self, _event: str):
            return lambda callback: callback

        async def connect(self, url: str, token: str) -> None:
            assert url == "ws://127.0.0.1:7880"
            assert token == "character-token"

        async def disconnect(self) -> None:
            return None

    class AudioSource:
        def __init__(self, sample_rate: int, channels: int) -> None:
            assert sample_rate == PCM_SAMPLE_RATE
            assert channels == PCM_CHANNELS

        async def capture_frame(self, frame: object) -> None:
            del frame

    class LocalAudioTrack:
        @staticmethod
        def create_audio_track(name: str, source: object) -> object:
            return (name, source)

    class TrackPublishOptions:
        def __init__(self, *, source: str) -> None:
            self.source = source

    microphone_source = "microphone"
    rtc = SimpleNamespace(
        Room=Room,
        AudioSource=AudioSource,
        LocalAudioTrack=LocalAudioTrack,
        TrackPublishOptions=TrackPublishOptions,
        TrackSource=SimpleNamespace(SOURCE_MICROPHONE=microphone_source),
    )
    monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(rtc=rtc))
    monkeypatch.setitem(sys.modules, "livekit.rtc", rtc)

    class Sessions:
        async def delete(self, session_id: str) -> None:
            del session_id

    class Rooms:
        async def delete(self, room_name: str) -> None:
            del room_name

    class CorePort:
        def notify(self, payload: bytes) -> None:
            del payload

    runtime = session_runtime.ProductionRuntimeManager(
        livekit_url="ws://127.0.0.1:7880",
        signer=RecordingSigner(),
        room_manager=Rooms(),
        session_repository=Sessions(),
        core_port=CorePort(),
        core_session_factory=NoopCoreSessionFactory(),
    )
    session_id = "20000000-0000-4000-8000-000000000010"

    async def exercise() -> None:
        await runtime.start_runtime(
            {
                "session_id": session_id,
                "identity": f"character-miori-{session_id}",
                "core_participant_id": "40000000-0000-4000-8000-000000000010",
                "reconnect_grace_ms": 60_000,
                "character_id": "miori",
                "conversation_id": "60000000-0000-4000-8000-000000000010",
            }
        )
        await runtime.stop(session_id)

    asyncio.run(exercise())

    assert token_requests == [
        {
            "identity": f"character-miori-{session_id}",
            "room": f"voice-{session_id}",
            "ttl_seconds": 90,
            "can_subscribe": True,
            "can_publish": True,
            "can_publish_data": True,
            "can_publish_sources": ["microphone"],
        }
    ]
    # 応答のないsessionには無所属のcharacter trackを作らない。
    assert published_tracks == []


def test_microphone_observer_returns_when_bridge_was_released(monkeypatch) -> None:
    stream_closed = False

    class AudioStream:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._emitted = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._emitted:
                raise StopAsyncIteration
            self._emitted = True
            return SimpleNamespace(
                frame=SimpleNamespace(
                    data=b"pcm",
                    samples_per_channel=1,
                )
            )

        async def aclose(self) -> None:
            nonlocal stream_closed
            stream_closed = True

    rtc = SimpleNamespace(AudioStream=AudioStream)
    monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(rtc=rtc))
    monkeypatch.setitem(sys.modules, "livekit.rtc", rtc)

    class Coordinator:
        generation = 1

        def is_current_participant(self, **_request: object) -> bool:
            return True

    async def publish_data(_payload: bytes, _topic: str) -> None:
        return None

    owner = session_owner(
        "released-session",
        room=object(),
        coordinator=Coordinator(),
        publish_data=publish_data,
    )
    readers = microphone_reader.MicrophoneReaderOwner(owner)

    asyncio.run(
        readers.observe(
            SimpleNamespace(sid="TR_released", get_stats=AsyncMock(return_value=[])),
            "user-identity",
            "participant-sid",
            1,
        )
    )

    assert stream_closed is True

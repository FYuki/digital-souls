from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import Awaitable
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import AsyncMock

import pytest

from tests.conversation_core_test_support import make_pcm16_wav
from tests.voice_capture_test_support import begin_capture, finish_capture, capture_harness


def _runtime_module(contract: str):
    module_name = "app.livekit_transport.runtime"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name is None or not (
            error.name == module_name or module_name.startswith(f"{error.name}.")
        ):
            raise
    pytest.fail(f"{module_name} must implement {contract}")


@dataclass
class RecordingObservationPort:
    records: list[dict[str, object]] = field(default_factory=list)

    def record(self, observation: dict[str, object]) -> None:
        self.records.append(observation)


class NoopCoreSession:
    async def end(self) -> None:
        return None


class NoopCoreSessionFactory:
    def create(self, **_request: object) -> NoopCoreSession:
        return NoopCoreSession()


def _runtime_shell(production):
    runtime = object.__new__(production.ProductionRuntimeManager)
    runtime._rooms = {}
    runtime._coordinators = {}
    runtime._session_tasks = {}
    runtime._participant_event_tails = {}
    runtime._ready = {}
    runtime._audio_sources = {}
    runtime._core_sessions = {}
    runtime._core_bridges = {}
    runtime._microphone_integrities = {}
    runtime._core_port = object()
    runtime._cleanup_states = {}
    return runtime


async def _drain_asyncio_tasks(tasks: set[asyncio.Task[None]]) -> None:
    """done callbackが次taskを作る直列queueをevent-loop turn単位で待つ。"""
    for _ in range(100):
        await asyncio.sleep(0)
        if not tasks:
            await asyncio.sleep(0)
            if not tasks:
                return
    raise AssertionError("asyncio tasks did not drain")


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
    production = importlib.import_module("app.livekit_transport.production")
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

    monkeypatch.setattr(production, "load_tts_config", load_tts_config)
    coordinator = RecordingCoordinator()
    delivery = production._ConversationCoreDelivery(
        coordinator=coordinator,
        audio_source=production._LiveKitPcmAudioSource(RtcAudioSource()),
        character_participant_id="40000000-0000-4000-8000-000000000010",
        character_id="miori",
    )
    factory = production.ProductionConversationCoreSessionFactory(
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
    production = importlib.import_module("app.livekit_transport.production")
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

    delivery = production._ConversationCoreDelivery(
        coordinator=Coordinator(),
        audio_source=production._LiveKitPcmAudioSource(RtcAudioSource()),
        character_participant_id="40000000-0000-4000-8000-000000000010",
        character_id="miori",
    )

    async def exercise() -> None:
        await delivery.publish(production.CoreEvent(
            type="response_audio_segment",
            session_id="20000000-0000-4000-8000-000000000010",
            response_id="50000000-0000-4000-8000-000000000010",
            audio_sequence=1,
            audio=b"\x01\x00" * 4,
            text_range=(0, 2),
        ))
        await delivery.publish(production.CoreEvent(
            type="response_cancelled",
            session_id="20000000-0000-4000-8000-000000000010",
            response_id="50000000-0000-4000-8000-000000000010",
            reason="user_request",
        ))
        await delivery.publish(production.CoreEvent(
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
    production = importlib.import_module("app.livekit_transport.production")
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
        delivery = production._ConversationCoreDelivery(
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
        event = production.CoreEvent(
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
    production = importlib.import_module("app.livekit_transport.production")
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
    bridge = production._ConversationCoreBridge(
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
    production = importlib.import_module("app.livekit_transport.production")
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

    bridge = production._ConversationCoreBridge(
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
    production = importlib.import_module("app.livekit_transport.production")
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

    bridge = production._ConversationCoreBridge(
        RecordingCoreSession(), schedule
    )
    preroll = bytes(
        index % 251
        for index in range(production.STT_MICROPHONE_PREROLL_BYTES + 257)
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
    retained_ms = production.STT_MICROPHONE_PREROLL_BYTES * 1000 / (16_000 * 2)
    assert 1600 <= retained_ms <= 2000
    assert requests[0]["audio"] == (
        preroll[-production.STT_MICROPHONE_PREROLL_BYTES :] + b"live"
    )


def test_production_core_bridge_previews_first_audio_before_speech_end() -> None:
    production = importlib.import_module("app.livekit_transport.production")
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

    bridge = production._ConversationCoreBridge(RecordingCoreSession(), schedule)

    async def exercise() -> None:
        bridge._begin_capture({
                    "type": "speech_started",
                    "speaker": {"role": "user"},
                    "utterance_id": "30000000-0000-4000-8000-000000000021",
                    "response_id": "50000000-0000-4000-8000-000000000021",
                })
        bridge.receive_microphone(b"x" * production.STT_TURN_PREVIEW_PCM_BYTES)
        await _drain_asyncio_tasks(tasks)

    asyncio.run(exercise())

    assert callable(previews[0].pop("input_is_current"))
    assert previews == [
        {
            "utterance_id": "30000000-0000-4000-8000-000000000021",
            "audio": b"x" * production.STT_TURN_PREVIEW_PCM_BYTES,
            "interrupted_response_id": "50000000-0000-4000-8000-000000000021",
        }
    ]


@pytest.mark.parametrize("prefix_samples", [0, 32_000])
def test_turn_preview_waits_for_audio_after_signal_onset(prefix_samples: int) -> None:
    """長い静音のpre-rollを800msの発話冒頭として数えない。"""
    production = importlib.import_module("app.livekit_transport.production")
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

    bridge = production._ConversationCoreBridge(Core(), schedule)
    voice = b"\x00\x10" * (production.STT_TURN_PREVIEW_PCM_BYTES // 2)

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
    production = importlib.import_module("app.livekit_transport.production")
    scheduled: list[Awaitable[None]] = []

    class Core:
        accepting_input = True

    core = Core()
    bridge = production._ConversationCoreBridge(core, scheduled.append)
    bridge._begin_capture({"type": "speech_started", "speaker": {"role": "user"},
                              "utterance_id": "preview-silence", "response_id": "old"})
    bridge.receive_microphone(bytes(production.STT_TURN_PREVIEW_PCM_BYTES * 2))
    try:
        assert scheduled == []
        core.accepting_input = False
        bridge.receive_microphone(b"\x00\x10" * production.STT_TURN_PREVIEW_PCM_BYTES)
        assert scheduled == []
    finally:
        for operation in scheduled:
            operation.close()


def test_production_core_bridge_stops_server_audio_for_playback_stop() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    scheduled: list[Awaitable[None]] = []
    stopped: list[str] = []

    class RecordingCoreSession:
        async def confirm_playback(self, **_request: object) -> bool:
            return True

    bridge = production._ConversationCoreBridge(
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
    production = importlib.import_module("app.livekit_transport.production")
    scheduled: list[Awaitable[None]] = []
    operations: list[str] = []

    class RejectingCoreSession:
        async def confirm_playback(self, **_request: object) -> bool:
            operations.append("confirm")
            raise ValueError("invalid playback prefix")

    bridge = production._ConversationCoreBridge(
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
    production = importlib.import_module("app.livekit_transport.production")
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

    bridge = production._ConversationCoreBridge(
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
    production = importlib.import_module("app.livekit_transport.production")
    tasks: set[asyncio.Task[None]] = set()

    class DisconnectedCoreSession:
        accepting_input = False

        def start_transcription(self, **_request: object) -> asyncio.Task[None]:
            raise AssertionError("切断後の発話をSTTへ渡してはならない")

    def schedule(operation) -> None:
        task = asyncio.create_task(operation)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    bridge = production._ConversationCoreBridge(
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
    production = importlib.import_module("app.livekit_transport.production")
    calls: list[dict[str, object]] = []
    scheduled: list[Awaitable[None]] = []

    class RecordingCoreSession:
        async def cancel_response(self, **request: object) -> None:
            calls.append(request)

        async def confirm_playback(self, **request: object) -> None:
            calls.append(request)

    bridge = production._ConversationCoreBridge(
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
    production = importlib.import_module("app.livekit_transport.production")
    discarded: list[dict[str, object]] = []
    scheduled: list[Awaitable[None]] = []

    class RecordingCoreSession:
        async def discard_utterance(self, **request: object) -> None:
            discarded.append(request)

    bridge = production._ConversationCoreBridge(
        RecordingCoreSession(), scheduled.append
    )
    utterance_ids = [
        f"30000000-0000-4000-8000-{index:012d}"
        for index in range(production.STT_MAX_OPEN_CAPTURES + 1)
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
    from app.livekit_transport.production import ProductionCoreEventInbox
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
    production = importlib.import_module("app.livekit_transport.production")
    inbox = production.ProductionCoreEventInbox()
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
    production = importlib.import_module("app.livekit_transport.production")
    inbox = production.ProductionCoreEventInbox()
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
    production = importlib.import_module("app.livekit_transport.production")
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
            assert sample_rate == production.PCM_SAMPLE_RATE
            assert channels == production.PCM_CHANNELS

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

    runtime = production.ProductionRuntimeManager(
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
    production = importlib.import_module("app.livekit_transport.production")
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

    runtime = _runtime_shell(production)

    asyncio.run(
        runtime._observe_microphone(
            "released-session",
            SimpleNamespace(sid="TR_released", get_stats=AsyncMock(return_value=[])),
            Coordinator(),
            "user-identity",
            "participant-sid",
            1,
            publish_data,
        )
    )

    assert stream_closed is True


@pytest.mark.parametrize("mode", ["disconnect", "generation_sync"])
def test_serialized_participant_events_and_generation_microphone_ownership(
    monkeypatch, mode,
) -> None:
    production = importlib.import_module("app.livekit_transport.production")
    session_id = "20000000-0000-4000-8000-000000000010"
    user_identity = f"user-{session_id}"
    callbacks: dict[str, object] = {}
    microphone_source = "microphone"
    remote_track = object()
    participant = SimpleNamespace(identity=user_identity, sid="PA_user")
    publication = SimpleNamespace(source=microphone_source)

    class RecordingSigner:
        async def issue_token(self, request: dict[str, object]) -> str:
            del request
            return "character-token"

    class LocalParticipant:
        async def publish_data(
            self, payload: bytes, *, reliable: bool, topic: str
        ) -> None:
            del payload, reliable, topic

        async def publish_track(self, track: object, options: object) -> None:
            del track, options

    class Room:
        sid = "RM_test"

        def __init__(self) -> None:
            self.local_participant = LocalParticipant()

        def on(self, event: str):
            def register(callback):
                callbacks[event] = callback
                return callback

            return register

        async def connect(self, url: str, token: str) -> None:
            assert url == "ws://127.0.0.1:7880"
            assert token == "character-token"
            connected = callbacks["participant_connected"]
            subscribed = callbacks["track_subscribed"]
            disconnected = callbacks["participant_disconnected"]
            assert callable(connected)
            assert callable(subscribed)
            assert callable(disconnected)
            connected(participant)
            subscribed(remote_track, publication, participant)
            if mode == "disconnect":
                disconnected(participant)

        async def disconnect(self) -> None:
            return None

    class AudioSource:
        def __init__(self, sample_rate: int, channels: int) -> None:
            assert sample_rate == production.PCM_SAMPLE_RATE
            assert channels == production.PCM_CHANNELS

        async def capture_frame(self, frame: object) -> None:
            del frame

    class LocalAudioTrack:
        @staticmethod
        def create_audio_track(name: str, source: object) -> object:
            return (name, source)

    class TrackPublishOptions:
        def __init__(self, *, source: str) -> None:
            self.source = source

    class AudioFrame:
        def __init__(self, *args: object) -> None:
            self.args = args

    rtc = SimpleNamespace(
        Room=Room,
        AudioSource=AudioSource,
        AudioFrame=AudioFrame,
        LocalAudioTrack=LocalAudioTrack,
        TrackPublishOptions=TrackPublishOptions,
        TrackSource=SimpleNamespace(SOURCE_MICROPHONE=microphone_source),
    )
    monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(rtc=rtc))
    monkeypatch.setitem(sys.modules, "livekit.rtc", rtc)

    class Sessions:
        async def delete(self, owned_session_id: str) -> None:
            assert owned_session_id == session_id

    class Rooms:
        async def delete(self, room_name: str) -> None:
            assert room_name == f"voice-{session_id}"

    class CorePort:
        def notify(self, payload: bytes) -> None:
            del payload

    runtime = production.ProductionRuntimeManager(
        livekit_url="ws://127.0.0.1:7880",
        signer=RecordingSigner(),
        room_manager=Rooms(),
        session_repository=Sessions(),
        core_port=CorePort(),
        core_session_factory=NoopCoreSessionFactory(),
    )

    async def exercise() -> None:
        observation_started = asyncio.Event()
        observations: list[tuple[str, str, object]] = []
        reader_events: list[tuple[str, int]] = []

        async def observe_microphone(
            owned_session_id: str,
            track: object,
            coordinator: object,
            participant_identity: str,
            participant_sid: str,
            generation: int,
            publish_data: object,
        ) -> None:
            del coordinator, publish_data
            observations.append((participant_identity, participant_sid, track))
            assert owned_session_id == session_id
            observation_started.set()
            reader_events.append(("started", generation))
            if mode == "generation_sync":
                try:
                    await asyncio.Event().wait()
                finally:
                    reader_events.append(("closed", generation))

        runtime._observe_microphone = observe_microphone
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
        await asyncio.wait_for(observation_started.wait(), timeout=0.5)
        coordinator = runtime._coordinators[session_id]

        if mode == "generation_sync":
            async def sync(generation: int) -> None:
                await coordinator.receive_data(identity=user_identity, participant_sid="PA_user",
                    topic="digital-souls.livekit-transport.v2", payload=json.dumps({
                        "protocol_version": "2.0", "type": "state_sync_request", "generation": generation,
                    }).encode())

            async def wait_for_event(event: tuple[str, int]) -> None:
                while event not in reader_events:
                    await asyncio.sleep(0)

            await sync(0)
            await asyncio.wait_for(wait_for_event(("started", 1)), timeout=0.5)
            assert reader_events == [("started", 0), ("closed", 0), ("started", 1)]
            # 古い世代の再送では現世代readerを重複作成しない。
            await sync(0)
            await asyncio.sleep(0)
            assert reader_events.count(("started", 1)) == 1
            callbacks["track_unsubscribed"](remote_track, publication, participant)
            await asyncio.wait_for(wait_for_event(("closed", 1)), timeout=0.5)
            await sync(1)
            await asyncio.sleep(0)
            assert len(observations) == 2
        else:
            async def wait_until_disconnected() -> None:
                while coordinator.phase != "unavailable":
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_until_disconnected(), timeout=0.5)

            assert observations == [(user_identity, "PA_user", remote_track)]
            assert not coordinator.is_current_participant(
                identity=user_identity, participant_sid="PA_user"
            )
        await runtime.stop(session_id)

    asyncio.run(exercise())


def test_production_stop_releases_local_ownership_before_external_cleanup_finishes() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    session_id = "20000000-0000-4000-8000-000000000010"

    async def exercise() -> None:
        disconnect_started = asyncio.Event()
        delete_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        deleted_sessions: list[str] = []

        class HangingRoom:
            async def disconnect(self) -> None:
                disconnect_started.set()
                await release_cleanup.wait()

        class Sessions:
            async def delete(self, owned_session_id: str) -> None:
                deleted_sessions.append(owned_session_id)

        class Rooms:
            async def delete(self, room_name: str) -> None:
                assert room_name == f"voice-{session_id}"
                delete_started.set()
                await release_cleanup.wait()

        runtime = _runtime_shell(production)
        runtime._sessions = Sessions()
        runtime._room_manager = Rooms()
        runtime._rooms = {session_id: HangingRoom()}
        runtime._coordinators = {}
        runtime._session_tasks = {session_id: set()}
        runtime._ready = {session_id: asyncio.Event()}
        runtime._audio_sources = {session_id: SimpleNamespace(aclose=AsyncMock())}
        runtime._cleanup_states = {}

        stop_task = asyncio.create_task(runtime.stop(session_id))
        await asyncio.wait_for(disconnect_started.wait(), timeout=0.5)
        await asyncio.wait_for(delete_started.wait(), timeout=0.5)

        assert deleted_sessions == [session_id]
        assert session_id not in runtime._rooms
        assert session_id not in runtime._session_tasks
        assert session_id not in runtime._ready
        assert session_id not in runtime._audio_sources

        release_cleanup.set()
        await asyncio.wait_for(stop_task, timeout=0.5)
        assert runtime._cleanup_states == {}

    asyncio.run(exercise())


def test_production_room_cleanup_survives_cancelled_stop() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    session_id = "20000000-0000-4000-8000-000000000010"

    async def exercise() -> None:
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()
        active_rooms = {f"voice-{session_id}"}
        delete_calls: list[str] = []

        class Sessions:
            async def delete(self, owned_session_id: str) -> None:
                assert owned_session_id == session_id

        class Rooms:
            async def delete(self, room_name: str) -> None:
                delete_calls.append(room_name)
                delete_started.set()
                await release_delete.wait()
                active_rooms.remove(room_name)

        class Room:
            async def disconnect(self) -> None:
                return None

        runtime = _runtime_shell(production)
        runtime._sessions = Sessions()
        runtime._room_manager = Rooms()
        runtime._rooms = {session_id: Room()}
        runtime._coordinators = {}
        runtime._session_tasks = {session_id: set()}
        runtime._ready = {session_id: asyncio.Event()}
        runtime._audio_sources = {session_id: SimpleNamespace(aclose=AsyncMock())}
        runtime._cleanup_states = {}

        stop_task = asyncio.create_task(runtime.stop(session_id))
        await asyncio.wait_for(delete_started.wait(), timeout=0.5)
        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop_task

        assert active_rooms == {f"voice-{session_id}"}
        release_delete.set()
        await asyncio.wait_for(runtime.stop_all(), timeout=0.5)

        assert active_rooms == set()
        assert delete_calls == [f"voice-{session_id}"]
        assert runtime._cleanup_states == {}

    asyncio.run(exercise())


def test_bootstrap_timeout_leaves_room_cleanup_owned_until_it_finishes() -> None:
    bootstrap = importlib.import_module("app.livekit_transport.bootstrap")
    production = importlib.import_module("app.livekit_transport.production")
    session_id = "20000000-0000-4000-8000-000000000010"

    async def exercise() -> None:
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()
        active_rooms: set[str] = set()

        class Room:
            async def disconnect(self) -> None:
                return None

        class Rooms:
            async def create(self, room_name: str) -> None:
                active_rooms.add(room_name)

            async def delete(self, room_name: str) -> None:
                delete_started.set()
                await release_delete.wait()
                active_rooms.remove(room_name)

        class Signer:
            async def issue(self, **request: object) -> str:
                raise AssertionError(f"user token must not be issued: {request}")

        class CorePort:
            def notify(self, payload: bytes) -> None:
                del payload

        sessions = bootstrap.InMemorySessionBindingRepository(
            session_id_factory=lambda: session_id
        )
        rooms = Rooms()
        signer = Signer()
        runtime = production.ProductionRuntimeManager(
            livekit_url="ws://127.0.0.1:7880",
            signer=signer,
            room_manager=rooms,
            session_repository=sessions,
            core_port=CorePort(),
        )

        async def connect(owned_session_id: str) -> None:
            runtime._rooms[owned_session_id] = Room()
            runtime._coordinators.pop(owned_session_id, None)
            runtime._session_tasks[owned_session_id] = set()
            runtime._ready[owned_session_id] = asyncio.Event()

        async def wait_until_ready(owned_session_id: str) -> None:
            raise RuntimeError(f"runtime {owned_session_id} is not ready")

        runtime.connect = connect
        runtime.wait_until_ready = wait_until_ready
        service = bootstrap.BootstrapService(
            session_repository=sessions,
            room_manager=rooms,
            runtime_manager=runtime,
            token_signer=signer,
            timeout_seconds=0.01,
        )
        request = {
            "protocol_version": "1.1",
            "request_id": "10000000-0000-4000-8000-000000000010",
            "character_id": "miori",
            "conversation_id": "20000000-0000-4000-8000-000000000011",
            "requested_reconnect_grace_ms": 60_000,
        }

        bootstrap_task = asyncio.create_task(service.bootstrap(request))
        await asyncio.wait_for(delete_started.wait(), timeout=0.5)
        with pytest.raises(bootstrap.BootstrapTimeoutError):
            await asyncio.wait_for(bootstrap_task, timeout=1.5)

        assert sessions.contains(session_id) is False
        assert runtime._rooms == {}
        assert active_rooms == {f"voice-{session_id}"}

        release_delete.set()
        await asyncio.wait_for(runtime.stop_all(), timeout=0.5)

        assert active_rooms == set()
        assert runtime._cleanup_states == {}

    asyncio.run(exercise())


def test_production_room_manager_accepts_already_deleted_room(monkeypatch) -> None:
    production = importlib.import_module("app.livekit_transport.production")

    class MissingRoomError(RuntimeError):
        code = "not_found"
        status = 404

    class RoomService:
        async def delete_room(self, request: object) -> None:
            assert request == "voice-session"
            raise MissingRoomError("room does not exist")

    class ApiClient:
        room = RoomService()

    class ApiModule:
        @staticmethod
        def DeleteRoomRequest(*, room: str) -> str:
            return room

    monkeypatch.setattr(production, "_livekit_api_module", lambda: ApiModule)

    asyncio.run(
        production.ProductionRoomManager(ApiClient()).delete("voice-session")
    )


def test_production_stop_all_retries_failed_room_cleanup() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    session_id = "20000000-0000-4000-8000-000000000010"

    async def exercise() -> None:
        active_rooms = {f"voice-{session_id}"}

        class Sessions:
            async def delete(self, owned_session_id: str) -> None:
                assert owned_session_id == session_id

        class Rooms:
            calls = 0

            async def delete(self, room_name: str) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("room deletion failed")
                active_rooms.remove(room_name)

        rooms = Rooms()
        runtime = _runtime_shell(production)
        runtime._sessions = Sessions()
        runtime._room_manager = rooms
        runtime._rooms = {}
        runtime._coordinators = {}
        runtime._session_tasks = {}
        runtime._ready = {}
        runtime._cleanup_states = {}

        from app.livekit_transport.errors import RoomCleanupPendingError

        with pytest.raises(RoomCleanupPendingError, match="room deletion failed"):
            await runtime.stop(session_id)

        assert active_rooms == {f"voice-{session_id}"}
        await runtime.stop_all()

        assert rooms.calls == 2
        assert active_rooms == set()
        assert runtime._cleanup_states == {}

    asyncio.run(exercise())


def test_production_stop_does_not_classify_local_failure_as_room_pending() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    session_id = "20000000-0000-4000-8000-000000000010"

    async def exercise() -> None:
        disconnect_calls = 0

        class Sessions:
            calls = 0

            async def delete(self, owned_session_id: str) -> None:
                assert owned_session_id == session_id
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("session binding deletion failed")

        class Rooms:
            calls = 0

            async def delete(self, room_name: str) -> None:
                assert room_name == f"voice-{session_id}"
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("room deletion failed")

        class Room:
            async def disconnect(self) -> None:
                nonlocal disconnect_calls
                disconnect_calls += 1

        rooms = Rooms()
        runtime = _runtime_shell(production)
        runtime._sessions = Sessions()
        runtime._room_manager = rooms
        runtime._rooms = {session_id: Room()}
        runtime._coordinators = {}
        runtime._session_tasks = {session_id: set()}
        runtime._ready = {session_id: asyncio.Event()}
        runtime._cleanup_states = {}

        with pytest.raises(
            RuntimeError, match="session binding deletion failed"
        ) as raised:
            await runtime.stop(session_id)

        from app.livekit_transport.errors import RoomCleanupPendingError

        assert not isinstance(raised.value, RoomCleanupPendingError)
        assert session_id in runtime._cleanup_states
        await runtime.stop_all()
        assert runtime._sessions.calls == 2
        assert rooms.calls == 2
        assert disconnect_calls == 1
        assert runtime._cleanup_states == {}

    asyncio.run(exercise())


def test_production_stop_retries_only_failed_room_disconnect() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    session_id = "20000000-0000-4000-8000-000000000010"

    async def exercise() -> None:
        class Sessions:
            calls = 0

            async def delete(self, owned_session_id: str) -> None:
                assert owned_session_id == session_id
                self.calls += 1

        class Rooms:
            calls = 0

            async def delete(self, room_name: str) -> None:
                assert room_name == f"voice-{session_id}"
                self.calls += 1

        class Room:
            calls = 0

            async def disconnect(self) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("room disconnect failed")

        sessions = Sessions()
        rooms = Rooms()
        room = Room()
        runtime = _runtime_shell(production)
        runtime._sessions = sessions
        runtime._room_manager = rooms
        runtime._rooms = {session_id: room}
        runtime._coordinators = {}
        runtime._session_tasks = {session_id: set()}
        runtime._ready = {session_id: asyncio.Event()}
        runtime._cleanup_states = {}

        with pytest.raises(RuntimeError, match="room disconnect failed"):
            await runtime.stop(session_id)

        await runtime.stop(session_id)

        assert sessions.calls == 1
        assert rooms.calls == 1
        assert room.calls == 2
        assert runtime._cleanup_states == {}

    asyncio.run(exercise())


@pytest.mark.parametrize("failure_stage", ["connect", "tts_prepare"])
def test_production_connect_failure_is_compensated_by_bootstrap_owner(
    monkeypatch, failure_stage,
) -> None:
    bootstrap = importlib.import_module("app.livekit_transport.bootstrap")
    production = importlib.import_module("app.livekit_transport.production")
    session_id = "20000000-0000-4000-8000-000000000010"
    disconnected: list[str] = []

    class LocalParticipant:
        async def publish_data(
            self, payload: bytes, *, reliable: bool, topic: str
        ) -> None:
            del payload, reliable, topic

    class FailingRoom:
        sid = "RM_test"

        def __init__(self) -> None:
            self.local_participant = LocalParticipant()

        def on(self, _event: str):
            return lambda callback: callback

        async def connect(self, _url: str, _token: str) -> None:
            if failure_stage == "connect":
                raise RuntimeError("room connection failed")

        async def disconnect(self) -> None:
            disconnected.append(session_id)

    rtc = SimpleNamespace(Room=FailingRoom)
    monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(rtc=rtc))
    monkeypatch.setitem(sys.modules, "livekit.rtc", rtc)

    class Signer:
        async def issue_token(self, _request: dict[str, object]) -> str:
            return "character-token"

        async def issue(self, **_request: object) -> str:
            raise AssertionError("user token must not be issued")

    class Rooms:
        def __init__(self) -> None:
            self.active: set[str] = set()

        async def create(self, room_name: str) -> None:
            self.active.add(room_name)

        async def delete(self, room_name: str) -> None:
            self.active.discard(room_name)

    class CorePort:
        def notify(self, payload: bytes) -> None:
            del payload

    sessions = bootstrap.InMemorySessionBindingRepository(
        session_id_factory=lambda: session_id
    )
    rooms = Rooms()
    signer = Signer()
    runtime = production.ProductionRuntimeManager(
        livekit_url="ws://127.0.0.1:7880",
        signer=signer,
        room_manager=rooms,
        session_repository=sessions,
        core_port=CorePort(),
    )
    closed_sources = []
    async def close_source():
        closed_sources.append(session_id)
    async def prepare_output(_room):
        return SimpleNamespace(aclose=close_source)
    async def fail_preparation(**_kwargs):
        raise RuntimeError("tts preparation failed")
    if failure_stage == "tts_prepare":
        runtime._prepare_output_track = prepare_output
        runtime._core_session_factory = SimpleNamespace(create=fail_preparation, create_ready=fail_preparation)
    service = bootstrap.BootstrapService(
        session_repository=sessions,
        room_manager=rooms,
        runtime_manager=runtime,
        token_signer=signer,
        timeout_seconds=0.1,
    )
    request = {
        "protocol_version": "1.1",
        "request_id": "10000000-0000-4000-8000-000000000010",
        "character_id": "miori",
        "conversation_id": "20000000-0000-4000-8000-000000000011",
        "requested_reconnect_grace_ms": 60_000,
    }

    with pytest.raises(RuntimeError, match="room connection failed|tts preparation failed"):
        asyncio.run(asyncio.wait_for(service.bootstrap(request), timeout=0.5))

    assert sessions.contains(session_id) is False
    assert rooms.active == set()
    assert runtime._rooms == {}
    assert runtime._coordinators == {}
    assert runtime._session_tasks == {}
    assert disconnected == [session_id]

    assert closed_sources == ([session_id] if failure_stage == "tts_prepare" else [])
    assert runtime._audio_sources == {}


def test_production_core_bridge_waits_for_each_utterances_integrity_without_a_media_tail():
    async def exercise():
        bridge, requests, tasks = capture_harness()
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        checks = []

        async def verify(track, start, end):
            index = len(checks)
            checks.append((track, start, end))
            entered[index].set()
            await release[index].wait()

        bridge._verify_audio_integrity = verify
        finishing = []
        try:
            for index, (name, pcm) in enumerate([("first", b"first-pcm!"), ("second", b"second-pcm")]):
                begin_capture(bridge, name)
                bridge.receive_microphone(pcm)
                finishing.append(asyncio.create_task(finish_capture(bridge, name)))
                await asyncio.wait_for(entered[index].wait(), 1)
            bridge.receive_microphone(b"next-preroll")
            release[1].set()
            await finishing[1]
            assert requests == []
            release[0].set()
            await finishing[0]
            await _drain_asyncio_tasks(tasks)
            assert [r["audio"] for r in requests] == [b"first-pcm!", b"second-pcm"]
            assert bridge._microphone_preroll == b"next-preroll"
        finally:
            for gate in release:
                gate.set()
            await asyncio.gather(*finishing, return_exceptions=True)
    asyncio.run(exercise())


@pytest.mark.parametrize("interruption", [False, True])
def test_backend_detection_clock_does_not_fabricate_client_onset_or_rebind_interruption(interruption) -> None:
    from app.livekit_transport import production
    from app.livekit_transport.measurement import LiveKitMeasurementSession

    events = []
    measurement = LiveKitMeasurementSession(
        session_id="session", character_id="miori", measurement_kind="controlled_baseline",
        record=events.append, clock_ns=lambda: 9_000_000_000,
    )
    if interruption:
        measurement.bind_response(response_id="old-response", source_utterance_ids=("old-utterance",))
    bridge = production._ConversationCoreBridge(NoopCoreSession(), lambda task: None, measurement=measurement)
    bridge._begin_capture({
        "type": "speech_started", "utterance_id": "new-utterance", "speaker": {"role": "user"},
        "monotonic_timestamp_ms": 1234,
        **({"response_id": "old-response"} if interruption else {}),
    })
    measurement.bind_response(response_id="new-response", source_utterance_ids=("new-utterance",))
    assert not any(event.name in {"vad_speech_start_client", "speech_started_client"} for event in events)
    starts = [event for event in events if event.name == "speech_started"]
    assert [event.response_id for event in starts] == (["old-response"] if interruption else [])
    for event in starts:
        assert event.utterance_id == "new-utterance"
        assert event.timestamp == 9_000_000_000
        assert event.clock_domain == "server_monotonic"
        assert event.unit == "nanosecond"


def test_core_bridge_prepares_the_same_preroll_for_preview_and_final_stt() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    calls: list[tuple[str, bytes]] = []
    tasks: list[asyncio.Task[None]] = []

    class Core:
        accepting_input = True

        def start_transcription(self, **request: object) -> asyncio.Task[None]:
            async def record() -> None:
                calls.append(("final", request["audio"]))
            task = asyncio.create_task(record())
            tasks.append(task)
            return task

        async def preview_turn(self, **request: object) -> None:
            calls.append(("preview", request["audio"]))

    bridge = production._ConversationCoreBridge(Core(), lambda operation: tasks.append(asyncio.create_task(operation)))
    original = bytes(32000 * 2) + b"\x00\x10" * 8000
    expected = bytes(5120 * 2) + b"\x00\x10" * 8000

    async def exercise() -> None:
        bridge._start_user_transcription("final", original)
        await asyncio.gather(*tasks)
        await bridge._preview_user_turn(utterance_id="preview", interrupted_response_id="old", microphone_pcm=original)

    asyncio.run(exercise())
    assert calls == [("final", expected), ("preview", expected)]


class PreparationCoreSession:
    def __init__(self, *, fail: bool = False) -> None:
        self.accepting_input = True
        self.fail = fail
        self.preparations = 0
        self.audio: list[bytes] = []

    async def prepare_transcription(self) -> bool:
        self.preparations += 1
        if self.fail:
            raise RuntimeError("optional preparation unavailable")
        return True

    def start_transcription(self, **request):
        self.audio.append(request["audio"])
        return asyncio.create_task(asyncio.sleep(0))




@pytest.mark.parametrize("fail", [False, True])
def test_bridge_prepares_once_after_contiguous_quiet_without_ending_or_trimming_audio(fail) -> None:
    async def exercise() -> None:
        production = importlib.import_module("app.livekit_transport.production")
        session = PreparationCoreSession(fail=fail)
        tasks: set[asyncio.Task[None]] = set()

        def schedule(operation):
            task = asyncio.create_task(operation)
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        bridge = production._ConversationCoreBridge(session, schedule)
        begin_capture(bridge, "preparation")
        # 290msの静音は準備しない。途中の有音で静音の連続時間をリセットする。
        pieces = [b"\x10\x01" * 160, bytes(16_000 * 2 * 290 // 1000),
                  b"\x10\x01" * 160, bytes(16_000 * 2 * 290 // 1000)]
        for pcm in pieces:
            bridge.receive_microphone(pcm)
        await _drain_asyncio_tasks(tasks)
        assert session.preparations == 0
        final_quiet = bytes(16_000 * 2 * 10 // 1000)
        bridge.receive_microphone(final_quiet)
        await _drain_asyncio_tasks(tasks)
        assert session.preparations == 1
        assert session.audio == []
        assert not bridge._user_audio_captures[0].finalized
        resumed = b"\x10\x01" * 160 + bytes(16_000)
        bridge.receive_microphone(resumed)
        await _drain_asyncio_tasks(tasks)
        assert session.preparations == 1
        await finish_capture(bridge, "preparation")
        await _drain_asyncio_tasks(tasks)
        assert session.audio == [b"".join(pieces) + final_quiet + resumed]

    asyncio.run(exercise())


def test_bridge_uses_retained_pcm_quiet_only_after_confirmed_speech_start() -> None:
    async def exercise() -> None:
        production = importlib.import_module("app.livekit_transport.production")
        session = PreparationCoreSession()
        tasks = []
        bridge = production._ConversationCoreBridge(session, lambda op: tasks.append(asyncio.create_task(op)))
        original = b"\x10\x01" * 160 + bytes(9600)
        bridge.receive_microphone(original)
        assert tasks == []
        assert session.preparations == 0
        begin_capture(bridge, "preparation")
        await asyncio.gather(*tasks)
        assert session.preparations == 1
        assert bytes(bridge._user_audio_captures[0].pcm) == original

    asyncio.run(exercise())


@pytest.mark.parametrize("gate", ["interruption", "active", "capacity", "ended", "finalized"])
def test_bridge_skips_preparation_when_not_safe(gate: str) -> None:
    async def exercise() -> None:
        production = importlib.import_module("app.livekit_transport.production")
        session = PreparationCoreSession()
        tasks = []
        bridge = production._ConversationCoreBridge(session, lambda op: tasks.append(asyncio.create_task(op)))
        begin_capture(bridge, "preparation", "old-response" if gate == "interruption" else None)
        capture = bridge._user_audio_captures[0]
        if gate == "active":
            bridge._transcription_active = True
        if gate == "capacity":
            capture.capacity_exceeded = True
        if gate == "ended":
            session.accepting_input = False
        if gate == "finalized":
            capture.finalized = True
        # 終了・欠落確認はこのSTT準備gate検査の対象外。
        bridge._consider_stt_preparation(capture, bytes(9600))
        await asyncio.gather(*tasks)
        assert session.preparations == 0

    asyncio.run(exercise())


def test_scheduled_preparation_does_not_start_after_session_ends() -> None:
    async def exercise() -> None:
        production = importlib.import_module("app.livekit_transport.production")
        session = PreparationCoreSession()
        tasks = []
        bridge = production._ConversationCoreBridge(session, lambda op: tasks.append(asyncio.create_task(op)))
        begin_capture(bridge, "preparation")
        bridge.receive_microphone(bytes(9600))
        assert len(tasks) == 1
        session.accepting_input = False
        await asyncio.gather(*tasks)
        assert session.preparations == 0

    asyncio.run(exercise())


def test_only_explicit_full_playback_confirmation_releases_output_wait():
    production = importlib.import_module("app.livekit_transport.production")
    scheduled, confirmations = [], []
    class Session:
        async def confirm_playback(self, **_request):
            # 最後のprefix自体は既に進捗通知で確認済みでも、全出力確認は受ける。
            return False
    bridge = production._ConversationCoreBridge(
        Session(), scheduled.append,
        confirm_response_playback=lambda response, sequence: confirmations.append((response, sequence)) or True,
    )
    for full in [False, True]:
        bridge.notify(json.dumps({"type":"playback_completed", "response_id":"old-response", "last_played_audio_sequence":2, "response_finished":full}).encode())
    async def exercise():
        for operation in scheduled:
            await operation
    asyncio.run(exercise())
    assert confirmations == [("old-response", 2)]


def test_cancel_transition_clock_uses_core_capture_in_trace_not_delivery_time() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    records = []
    wire = []
    async def send(payload):
        wire.append(json.loads(payload))
    delivery = production._ConversationCoreDelivery(
        coordinator=SimpleNamespace(send_core=send), audio_source=SimpleNamespace(clear=lambda _: None),
        character_participant_id="40000000-0000-4000-8000-000000000010", character_id="miori",
    )
    delivery.attach_measurement(SimpleNamespace(record_response_event=lambda **row: records.append(row)))
    asyncio.run(delivery.publish(production.CoreEvent(type="response_cancelled",
        session_id="20000000-0000-4000-8000-000000000010", response_id="50000000-0000-4000-8000-000000000010",
        reason="barge_in", terminal_state_bounds_ns=(1_234_000, 1_234_900))))
    assert [(r["name"], r.get("timestamp")) for r in records] == [
        ("cancel_state_lower", 1_234_000), ("cancel_state_upper", 1_234_900),
        ("response_excluded", None), ("response_cancelled", None)]
    assert records[-1]["outcome"] == "excluded"
    assert records[-1]["reason_code"] == "barge_in"
    assert all("terminal_state_bounds_ns" not in event for event in wire)


@pytest.mark.parametrize("waiting_stage", ["connect", "output", "tts"])
def test_join_expiry_during_startup_does_not_restore_cleaned_resources(monkeypatch, waiting_stage):
    from uuid import uuid4
    production = importlib.import_module("app.livekit_transport.production")

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        closed, ended, deleted = [], [], []
        async def pause(stage):
            if waiting_stage == stage:
                entered.set()
                await release.wait()
        class Room:
            def __init__(self):
                self.local_participant = SimpleNamespace(publish_data=lambda *args, **kwargs: None)
            def on(self, _event):
                return lambda callback: callback
            async def connect(self, *_args):
                await pause("connect")
            async def disconnect(self):
                pass
        rtc = SimpleNamespace(Room=Room)
        monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(rtc=rtc))
        monkeypatch.setitem(sys.modules, "livekit.rtc", rtc)
        async def token(_request):
            return "character-token"
        async def delete(identifier):
            deleted.append(identifier)
        async def close_source():
            closed.append("audio")
        async def prepare(_room):
            await pause("output")
            return SimpleNamespace(aclose=close_source, clear=lambda: None)
        class Core(NoopCoreSession):
            async def end(self):
                ended.append("core")
        async def create(**_kwargs):
            await pause("tts")
            return Core()
        runtime = production.ProductionRuntimeManager(
            livekit_url="ws://127.0.0.1:7880",
            signer=SimpleNamespace(issue_token=token),
            room_manager=SimpleNamespace(delete=delete),
            session_repository=SimpleNamespace(delete=delete),
            core_port=SimpleNamespace(notify=lambda _payload: None),
            core_session_factory=SimpleNamespace(create=create, create_ready=create),
        )
        runtime._prepare_output_track = prepare
        session_id = str(uuid4())
        pending = asyncio.create_task(runtime.start_runtime({
            "session_id": session_id, "identity": "character",
            "character_id": "miori", "conversation_id": str(uuid4()),
            "core_participant_id": str(uuid4()), "reconnect_grace_ms": 60_000,
        }))
        await asyncio.wait_for(entered.wait(), 1)
        coordinator = runtime._coordinators[session_id]
        # 90秒待たずに、実際のjoin期限処理とcleanupを通す。
        await coordinator._expire_after(0)
        release.set()
        with pytest.raises(RuntimeError, match="runtime startup ended"):
            await asyncio.wait_for(pending, 1)
        assert runtime._core_sessions == {}
        assert runtime._core_bridges == {}
        assert runtime._audio_sources == {}
        assert runtime._coordinators == {}
        assert runtime._rooms == {}
        assert runtime._ready == {}
        assert runtime._cleanup_states == {}
        assert closed == ([] if waiting_stage == "connect" else ["audio"])
        assert ended == (["core"] if waiting_stage == "tts" else [])
        assert len(deleted) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["eof", "exception", "invalid_frame", "cancel"])
def test_microphone_reader_termination_closes_input_and_releases_monitor(monkeypatch, ending):
    """無音中のEOF・例外・不正frameでも、終了処理と入力停止を省略しない。"""
    production = importlib.import_module("app.livekit_transport.production")
    stream_closed = False
    closed_tracks = []
    class AudioStream:
        def __init__(self, *_args, **_kwargs):
            self.emitted = False
        def __aiter__(self):
            return self
        async def __anext__(self):
            if ending == "eof" or self.emitted:
                raise StopAsyncIteration
            self.emitted = True
            if ending == "exception":
                raise RuntimeError("injected stream failure")
            if ending == "cancel":
                raise asyncio.CancelledError
            return SimpleNamespace(frame=SimpleNamespace(
                sample_rate=8000, num_channels=1, samples_per_channel=160,
                data=memoryview(bytes(320)), userdata={},
            ))
        async def aclose(self):
            nonlocal stream_closed
            stream_closed = True
    rtc = SimpleNamespace(AudioStream=AudioStream)
    monkeypatch.setattr(production, "_livekit_rtc_module", lambda: rtc)
    class Coordinator:
        generation = 1
        def is_current_participant(self, **kwargs):
            return True
    class Bridge:
        def close_microphone_track(self, sid):
            closed_tracks.append(sid)
        async def receive_microphone_frame(self, *args, **kwargs):
            pytest.fail("不正なframeをVADへ配送した")
    async def publish_data(*args):
        pass
    async def scenario():
        runtime = _runtime_shell(production)
        runtime._core_bridges["session"] = Bridge()
        operation = runtime._observe_microphone(
            "session", SimpleNamespace(sid="TR_first", get_stats=AsyncMock(return_value=[])),
            Coordinator(), "user", "participant", 1, publish_data,
        )
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            await operation
        assert closed_tracks == ["TR_first"]
        assert stream_closed
        assert runtime._microphone_integrities == {}
    asyncio.run(scenario())

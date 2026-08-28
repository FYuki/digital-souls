from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import Awaitable
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID

import pytest

from tests.conversation_core_test_support import make_pcm16_wav


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
    runtime._core_port = object()
    runtime._cleanup_states = {}
    return runtime


def test_microphone_observation_records_metadata_without_audio_bytes() -> None:
    module = _runtime_module("metadata-only microphone observation")
    port = RecordingObservationPort()
    observer = module.MicrophoneTrackObserver(observation_port=port)

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


def test_voicevox_wav_reaches_livekit_as_matching_48khz_pcm_frame(
    monkeypatch,
) -> None:
    production = importlib.import_module("app.livekit_transport.production")
    session_id = "20000000-0000-4000-8000-000000000010"
    events: list[tuple[str, object]] = []
    response_ids: list[str] = []

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

        async def send_core(self, payload: bytes) -> None:
            events.append(("metadata", json.loads(payload)))

    class RtcAudioSource:
        async def capture_frame(self, frame: AudioFrame) -> None:
            events.append(("frame", frame))

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

    def generate_reply(
        character_id: str,
        history_session: object,
        transcript: str,
    ) -> str:
        assert character_id == "miori"
        assert isinstance(history_session, HistorySession)
        assert transcript == "利用者の発話"
        return "光織の応答"

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
        generate_reply=generate_reply,
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


def test_response_started_character_speaker_passes_schema_validation() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    coordinator_module = importlib.import_module("app.livekit_transport.coordinator")
    session_id = "20000000-0000-4000-8000-000000000010"
    participant_id = "40000000-0000-4000-8000-000000000010"
    published: list[bytes] = []

    class CorePort:
        def notify(self, payload: bytes) -> None:
            del payload

    class AudioSource:
        async def publish(self, pcm: bytes) -> None:
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
        event = production.CoreEvent(
            type="response_started",
            session_id=session_id,
            response_id="50000000-0000-4000-8000-000000000010",
            source_utterance_ids=(
                "30000000-0000-4000-8000-000000000010",
            ),
        )

        await delivery.publish(event)
        payload = json.loads(published[0])
        coordinator.acknowledge(str(payload["event_id"]), "character_to_user")
        await coordinator.cleanup("test_complete")

    asyncio.run(exercise())

    payload = json.loads(published[0])
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
    bridge = production._ConversationCoreBridge(session, schedule)

    async def exercise() -> None:
        common = {
            "protocol_version": "1.0",
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
                "speaker": user_speaker,
                "utterance_id": "30000000-0000-4000-8000-000000000010",
            }
        ).encode()
        delivery.decode_core_event(speech_started)
        bridge.notify(speech_started)
        bridge.receive_microphone(b"live-pcm")
        utterance_finalized = json.dumps(
            {
                **common,
                "event_id": "10000000-0000-4000-8000-000000000011",
                "type": "utterance_finalized",
                "speaker": user_speaker,
                "utterance_id": "30000000-0000-4000-8000-000000000010",
                "transcript": "client transcript",
                "should_response": False,
            }
        ).encode()
        delivery.decode_core_event(utterance_finalized)
        bridge.notify(utterance_finalized)
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
        while tasks:
            await asyncio.gather(*tuple(tasks))
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


def test_production_core_bridge_keeps_pcm_owned_by_each_consecutive_utterance() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    calls: list[dict[str, object]] = []
    scheduled: list[Awaitable[None]] = []
    transcription_tasks: set[asyncio.Task[None]] = set()

    class RecordingCoreSession:
        def start_transcription(self, **request: object) -> asyncio.Task[None]:
            async def record() -> None:
                calls.append(request)

            task = asyncio.create_task(record())
            transcription_tasks.add(task)
            task.add_done_callback(transcription_tasks.discard)
            return task

    bridge = production._ConversationCoreBridge(
        RecordingCoreSession(), scheduled.append
    )
    common = {
        "protocol_version": "1.0",
        "session_id": "20000000-0000-4000-8000-000000000010",
        "monotonic_timestamp_ms": 1_000,
        "speaker": {
            "participant_id": "40000000-0000-4000-8000-000000000010",
            "role": "user",
        },
    }

    async def exercise() -> None:
        for suffix, pcm in (("10", b"pcm-one"), ("11", b"pcm-two")):
            utterance_id = f"30000000-0000-4000-8000-0000000000{suffix}"
            bridge.notify(
                json.dumps(
                    {
                        **common,
                        "event_id": f"10000000-0000-4000-8000-0000000000{suffix}",
                        "type": "speech_started",
                        "utterance_id": utterance_id,
                    }
                ).encode()
            )
            bridge.receive_microphone(pcm)
            bridge.notify(
                json.dumps(
                    {
                        **common,
                        "event_id": f"11000000-0000-4000-8000-0000000000{suffix}",
                        "type": "utterance_finalized",
                        "utterance_id": utterance_id,
                        "transcript": "client transcript",
                        "should_response": False,
                    }
                ).encode()
            )

        assert len(scheduled) == 2
        await scheduled[0]
        await scheduled[1]
        while transcription_tasks:
            await asyncio.gather(*tuple(transcription_tasks))

    asyncio.run(exercise())

    assert calls == [
        {
            "utterance_id": "30000000-0000-4000-8000-000000000010",
            "audio": b"pcm-one",
            "should_response": True,
        },
        {
            "utterance_id": "30000000-0000-4000-8000-000000000011",
            "audio": b"pcm-two",
            "should_response": True,
        },
    ]


def test_production_core_bridge_keeps_late_pcm_with_earliest_utterance() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    calls: list[dict[str, object]] = []
    scheduled: list[Awaitable[None]] = []
    transcription_tasks: set[asyncio.Task[None]] = set()

    class RecordingCoreSession:
        def start_transcription(self, **request: object) -> asyncio.Task[None]:
            async def record() -> None:
                calls.append(request)

            task = asyncio.create_task(record())
            transcription_tasks.add(task)
            task.add_done_callback(transcription_tasks.discard)
            return task

    bridge = production._ConversationCoreBridge(
        RecordingCoreSession(), scheduled.append
    )
    common = {
        "protocol_version": "1.0",
        "session_id": "20000000-0000-4000-8000-000000000010",
        "monotonic_timestamp_ms": 1_000,
        "speaker": {
            "participant_id": "40000000-0000-4000-8000-000000000010",
            "role": "user",
        },
    }
    first_utterance_id = "30000000-0000-4000-8000-000000000010"
    second_utterance_id = "30000000-0000-4000-8000-000000000011"

    def notify(event_id: str, event_type: str, utterance_id: str) -> None:
        event: dict[str, object] = {
            **common,
            "event_id": event_id,
            "type": event_type,
            "utterance_id": utterance_id,
        }
        if event_type == "utterance_finalized":
            event.update(transcript="client transcript", should_response=False)
        bridge.notify(json.dumps(event).encode())

    async def exercise() -> None:
        notify(
            "10000000-0000-4000-8000-000000000010",
            "speech_started",
            first_utterance_id,
        )
        notify(
            "11000000-0000-4000-8000-000000000010",
            "utterance_finalized",
            first_utterance_id,
        )
        notify(
            "10000000-0000-4000-8000-000000000011",
            "speech_started",
            second_utterance_id,
        )
        bridge.receive_microphone(b"pcm-one")
        notify(
            "11000000-0000-4000-8000-000000000011",
            "utterance_finalized",
            second_utterance_id,
        )
        bridge.receive_microphone(b"pcm-two")

        assert len(scheduled) == 2
        for operation in scheduled:
            await operation
        while transcription_tasks:
            await asyncio.gather(*tuple(transcription_tasks))

    asyncio.run(exercise())

    assert calls == [
        {
            "utterance_id": first_utterance_id,
            "audio": b"pcm-one",
            "should_response": True,
        },
        {
            "utterance_id": second_utterance_id,
            "audio": b"pcm-two",
            "should_response": True,
        },
    ]


def test_production_core_bridge_separates_overlapping_utterance_pcm() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    calls: list[dict[str, object]] = []
    scheduled: list[Awaitable[None]] = []
    transcription_tasks: set[asyncio.Task[None]] = set()

    class RecordingCoreSession:
        def start_transcription(self, **request: object) -> asyncio.Task[None]:
            async def record() -> None:
                calls.append(request)

            task = asyncio.create_task(record())
            transcription_tasks.add(task)
            task.add_done_callback(transcription_tasks.discard)
            return task

    bridge = production._ConversationCoreBridge(
        RecordingCoreSession(), scheduled.append
    )
    common = {
        "speaker": {"role": "user"},
        "transcript": "client transcript",
        "should_response": False,
    }
    first_id = "30000000-0000-4000-8000-000000000010"
    second_id = "30000000-0000-4000-8000-000000000011"

    def notify(event_type: str, utterance_id: str) -> None:
        bridge.notify(
            json.dumps(
                {**common, "type": event_type, "utterance_id": utterance_id}
            ).encode()
        )

    async def exercise() -> None:
        notify("speech_started", first_id)
        bridge.receive_microphone(b"pcm-one")
        notify("speech_started", second_id)
        bridge.receive_microphone(b"pcm-two")
        notify("utterance_finalized", first_id)
        notify("utterance_finalized", second_id)
        for operation in scheduled:
            await operation
        while transcription_tasks:
            await asyncio.gather(*tuple(transcription_tasks))

    asyncio.run(exercise())

    assert calls == [
        {"utterance_id": first_id, "audio": b"pcm-one", "should_response": True},
        {"utterance_id": second_id, "audio": b"pcm-two", "should_response": True},
    ]


def test_production_core_bridge_empty_capture_does_not_block_later_audio() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    calls: list[dict[str, object]] = []
    scheduled: list[Awaitable[None]] = []
    transcription_tasks: set[asyncio.Task[None]] = set()

    class RecordingCoreSession:
        def start_transcription(self, **request: object) -> asyncio.Task[None]:
            async def record() -> None:
                calls.append(request)

            task = asyncio.create_task(record())
            transcription_tasks.add(task)
            task.add_done_callback(transcription_tasks.discard)
            return task

    bridge = production._ConversationCoreBridge(
        RecordingCoreSession(), scheduled.append
    )
    first_id = "30000000-0000-4000-8000-000000000010"
    second_id = "30000000-0000-4000-8000-000000000011"

    def notify(event_type: str, utterance_id: str) -> None:
        bridge.notify(
            json.dumps(
                {
                    "type": event_type,
                    "utterance_id": utterance_id,
                    "speaker": {"role": "user"},
                }
            ).encode()
        )

    async def exercise() -> None:
        notify("speech_started", first_id)
        notify("speech_started", second_id)
        bridge.receive_microphone(b"pcm-two")
        notify("utterance_finalized", first_id)
        notify("utterance_finalized", second_id)
        for operation in scheduled:
            await operation
        while transcription_tasks:
            await asyncio.gather(*tuple(transcription_tasks))

    asyncio.run(exercise())

    assert calls == [
        {"utterance_id": second_id, "audio": b"pcm-two", "should_response": True}
    ]


def test_production_core_bridge_starts_transcription_for_either_input_order() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    utterance_id = "30000000-0000-4000-8000-000000000010"
    pcm = b"same-pcm"

    async def exercise(finalized_first: bool) -> list[dict[str, object]]:
        calls: list[dict[str, object]] = []
        scheduled: list[Awaitable[None]] = []
        transcription_tasks: set[asyncio.Task[None]] = set()

        class RecordingCoreSession:
            def start_transcription(self, **request: object) -> asyncio.Task[None]:
                async def record() -> None:
                    calls.append(request)

                task = asyncio.create_task(record())
                transcription_tasks.add(task)
                task.add_done_callback(transcription_tasks.discard)
                return task

        bridge = production._ConversationCoreBridge(
            RecordingCoreSession(), scheduled.append
        )
        common = {
            "protocol_version": "1.0",
            "session_id": "20000000-0000-4000-8000-000000000010",
            "monotonic_timestamp_ms": 1_000,
            "speaker": {
                "participant_id": "40000000-0000-4000-8000-000000000010",
                "role": "user",
            },
            "utterance_id": utterance_id,
        }
        bridge.notify(
            json.dumps(
                {
                    **common,
                    "event_id": "10000000-0000-4000-8000-000000000010",
                    "type": "speech_started",
                }
            ).encode()
        )
        finalized = json.dumps(
            {
                **common,
                "event_id": "10000000-0000-4000-8000-000000000011",
                "type": "utterance_finalized",
                "transcript": "client transcript",
                "should_response": False,
            }
        ).encode()

        if finalized_first:
            bridge.notify(finalized)
            bridge.receive_microphone(pcm)
        else:
            bridge.receive_microphone(pcm)
            bridge.notify(finalized)

        for operation in scheduled:
            await operation
        while transcription_tasks:
            await asyncio.gather(*tuple(transcription_tasks))
        return calls

    expected = [
        {
            "utterance_id": utterance_id,
            "audio": pcm,
            "should_response": True,
        }
    ]
    assert asyncio.run(exercise(finalized_first=False)) == expected
    assert asyncio.run(exercise(finalized_first=True)) == expected


def test_production_core_bridge_accepts_pcm_after_prebind_finalized_events() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    session_id = "20000000-0000-4000-8000-000000000010"
    utterance_id = "30000000-0000-4000-8000-000000000010"
    calls: list[dict[str, object]] = []
    scheduled: list[Awaitable[None]] = []
    transcription_tasks: set[asyncio.Task[None]] = set()

    class RecordingCoreSession:
        def start_transcription(self, **request: object) -> asyncio.Task[None]:
            async def record() -> None:
                calls.append(request)

            task = asyncio.create_task(record())
            transcription_tasks.add(task)
            task.add_done_callback(transcription_tasks.discard)
            return task

    bridge = production._ConversationCoreBridge(
        RecordingCoreSession(), scheduled.append
    )
    inbox = production.ProductionCoreEventInbox()
    common = {
        "protocol_version": "1.0",
        "session_id": session_id,
        "monotonic_timestamp_ms": 1_000,
        "speaker": {
            "participant_id": "40000000-0000-4000-8000-000000000010",
            "role": "user",
        },
        "utterance_id": utterance_id,
    }
    inbox.notify(
        json.dumps(
            {
                **common,
                "event_id": "10000000-0000-4000-8000-000000000010",
                "type": "speech_started",
            }
        ).encode()
    )
    inbox.notify(
        json.dumps(
            {
                **common,
                "event_id": "10000000-0000-4000-8000-000000000011",
                "type": "utterance_finalized",
                "transcript": "client transcript",
                "should_response": False,
            }
        ).encode()
    )

    async def exercise() -> None:
        inbox.bind(session_id, bridge.notify)
        bridge.receive_microphone(b"late-pcm")
        for operation in scheduled:
            await operation
        while transcription_tasks:
            await asyncio.gather(*tuple(transcription_tasks))

    asyncio.run(exercise())

    assert calls == [
        {
            "utterance_id": utterance_id,
            "audio": b"late-pcm",
            "should_response": True,
        }
    ]


def test_production_core_bridge_does_not_use_client_text_without_pcm() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    scheduled_tasks: set[asyncio.Task[None]] = set()

    class RecordingCoreSession:
        def start_transcription(self, **request: object) -> asyncio.Task[None]:
            raise AssertionError(f"STT must not start without PCM: {request!r}")

        async def finalize_utterance(self, **request: object) -> None:
            raise AssertionError(f"client text must not reach Core: {request!r}")

    def schedule(operation: Awaitable[None]) -> None:
        task = asyncio.create_task(operation)
        scheduled_tasks.add(task)
        task.add_done_callback(scheduled_tasks.discard)

    bridge = production._ConversationCoreBridge(RecordingCoreSession(), schedule)
    user_speaker = {
        "participant_id": "40000000-0000-4000-8000-000000000010",
        "role": "user",
    }

    async def exercise() -> None:
        bridge.notify(
            json.dumps(
                {
                    "protocol_version": "1.0",
                    "event_id": "10000000-0000-4000-8000-000000000010",
                    "session_id": "20000000-0000-4000-8000-000000000010",
                    "monotonic_timestamp_ms": 1_000,
                    "type": "speech_started",
                    "speaker": user_speaker,
                    "utterance_id": "30000000-0000-4000-8000-000000000010",
                }
            ).encode()
        )
        bridge.notify(
            json.dumps(
                {
                    "protocol_version": "1.0",
                    "event_id": "10000000-0000-4000-8000-000000000011",
                    "session_id": "20000000-0000-4000-8000-000000000010",
                    "monotonic_timestamp_ms": 1_001,
                    "type": "utterance_finalized",
                    "speaker": user_speaker,
                    "utterance_id": "30000000-0000-4000-8000-000000000010",
                    "transcript": "偽のclient本文",
                    "should_response": False,
                }
            ).encode()
        )
        while scheduled_tasks:
            await asyncio.gather(*tuple(scheduled_tasks))

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
    assert len(published_tracks) == 1
    assert published_tracks[0][1].source == microphone_source


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
            object(),
            Coordinator(),
            "user-identity",
            "participant-sid",
            1,
            publish_data,
        )
    )

    assert stream_closed is True


def test_serialized_participant_events_handle_track_before_immediate_disconnect(
    monkeypatch,
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

        async def observe_microphone(
            owned_session_id: str,
            track: object,
            coordinator: object,
            participant_identity: str,
            participant_sid: str,
            generation: int,
            publish_data: object,
        ) -> None:
            del coordinator, generation, publish_data
            observations.append((participant_identity, participant_sid, track))
            assert owned_session_id == session_id
            observation_started.set()

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
        runtime._audio_sources = {session_id: object()}
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
        runtime._audio_sources = {session_id: object()}
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
            "protocol_version": "1.0",
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


def test_production_connect_failure_is_compensated_by_bootstrap_owner(
    monkeypatch,
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
    service = bootstrap.BootstrapService(
        session_repository=sessions,
        room_manager=rooms,
        runtime_manager=runtime,
        token_signer=signer,
        timeout_seconds=0.1,
    )
    request = {
        "protocol_version": "1.0",
        "request_id": "10000000-0000-4000-8000-000000000010",
        "character_id": "miori",
        "conversation_id": "20000000-0000-4000-8000-000000000011",
        "requested_reconnect_grace_ms": 60_000,
    }

    with pytest.raises(RuntimeError, match="room connection failed"):
        asyncio.run(asyncio.wait_for(service.bootstrap(request), timeout=0.5))

    assert sessions.contains(session_id) is False
    assert rooms.active == set()
    assert runtime._rooms == {}
    assert runtime._coordinators == {}
    assert runtime._session_tasks == {}
    assert disconnected == [session_id]

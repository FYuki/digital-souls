from __future__ import annotations

import asyncio
import importlib
import json
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest


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


@dataclass
class RecordingAudioSource:
    events: list[tuple[str, object]]

    async def publish(self, pcm: bytes) -> None:
        self.events.append(("pcm", pcm))


@dataclass
class RecordingMetadataPort:
    events: list[tuple[str, object]]

    async def publish(self, metadata: dict[str, object]) -> None:
        self.events.append(("metadata", metadata))


def _runtime_shell(production):
    runtime = object.__new__(production.ProductionRuntimeManager)
    runtime._rooms = {}
    runtime._coordinators = {}
    runtime._session_tasks = {}
    runtime._participant_event_tails = {}
    runtime._ready = {}
    runtime._fixture_sources = {}
    runtime._fixture_generations = {}
    runtime._fixture_locks = {}
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


def test_pcm_fixture_publish_keeps_logical_metadata_separate_from_media() -> None:
    module = _runtime_module("separate PCM publication and logical metadata")
    events: list[tuple[str, object]] = []
    source = RecordingAudioSource(events)
    publisher = module.PcmFixturePublisher(
        audio_source=source,
        metadata_port=RecordingMetadataPort(events),
    )
    pcm = bytes(range(32))

    asyncio.run(
        publisher.publish(
            response_id="30000000-0000-4000-8000-000000000001",
            audio_sequence=0,
            generation=2,
            pcm=pcm,
            sample_count=16,
        )
    )

    assert events[0][0] == "metadata"
    assert events[1] == ("pcm", pcm)
    metadata = events[0][1]
    assert isinstance(metadata, dict)
    assert metadata == {
        "type": "logical_audio_segment",
        "response_id": "30000000-0000-4000-8000-000000000001",
        "audio_sequence": 0,
        "generation": 2,
        "pcm_sample_count": 16,
    }
    assert pcm not in metadata.values()


def test_production_fixture_registers_and_publishes_one_response_per_generation() -> None:
    production = importlib.import_module("app.livekit_transport.production")
    session_id = "20000000-0000-4000-8000-000000000010"
    events: list[tuple[str, object]] = []
    runtime = _runtime_shell(production)
    runtime._fixture_generations = {}
    runtime._fixture_sources = {session_id: RecordingAudioSource(events)}
    runtime._fixture_locks = {session_id: asyncio.Lock()}

    class RecordingCoordinator:
        generation = 0

        def __init__(self) -> None:
            self.response_ids: list[str] = []

        def begin_response(self, *, response_id: str) -> None:
            self.response_ids.append(response_id)

    coordinator = RecordingCoordinator()

    async def publish_data(payload: bytes, topic: str) -> None:
        events.append((topic, json.loads(payload)))

    async def exercise() -> None:
        await asyncio.gather(
            runtime._publish_fixture(session_id, coordinator, publish_data),
            runtime._publish_fixture(session_id, coordinator, publish_data),
        )
        coordinator.generation = 1
        await runtime._publish_fixture(session_id, coordinator, publish_data)

    asyncio.run(exercise())

    metadata = [value for kind, value in events if kind == production.PRIVATE_TOPIC]
    assert len(coordinator.response_ids) == 2
    assert [frame["response_id"] for frame in metadata] == coordinator.response_ids
    assert coordinator.response_ids[0] != coordinator.response_ids[1]
    assert coordinator.response_ids == [
        production._fixture_response_id(session_id, 0),
        production._fixture_response_id(session_id, 1),
    ]


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
    )
    session_id = "20000000-0000-4000-8000-000000000010"

    async def exercise() -> None:
        await runtime.start_runtime(
            {
                "session_id": session_id,
                "identity": f"character-miori-{session_id}",
                "core_participant_id": "40000000-0000-4000-8000-000000000010",
                "reconnect_grace_ms": 60_000,
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
        runtime._fixture_sources = {session_id: object()}
        runtime._fixture_generations = {session_id: 0}
        runtime._fixture_locks = {session_id: asyncio.Lock()}
        runtime._cleanup_states = {}

        stop_task = asyncio.create_task(runtime.stop(session_id))
        await asyncio.wait_for(disconnect_started.wait(), timeout=0.5)
        await asyncio.wait_for(delete_started.wait(), timeout=0.5)

        assert deleted_sessions == [session_id]
        assert session_id not in runtime._rooms
        assert session_id not in runtime._session_tasks
        assert session_id not in runtime._ready
        assert session_id not in runtime._fixture_sources
        assert session_id not in runtime._fixture_generations
        assert session_id not in runtime._fixture_locks

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
        runtime._fixture_sources = {session_id: object()}
        runtime._fixture_generations = {session_id: 0}
        runtime._fixture_locks = {session_id: asyncio.Lock()}
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
            runtime._fixture_locks[owned_session_id] = asyncio.Lock()

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
        runtime._fixture_sources = {}
        runtime._fixture_generations = {}
        runtime._fixture_locks = {}
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
        runtime._fixture_sources = {}
        runtime._fixture_generations = {}
        runtime._fixture_locks = {session_id: asyncio.Lock()}
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
        runtime._fixture_sources = {}
        runtime._fixture_generations = {}
        runtime._fixture_locks = {session_id: asyncio.Lock()}
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

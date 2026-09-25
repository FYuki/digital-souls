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

@pytest.mark.parametrize("mode", ["disconnect", "generation_sync"])
def test_serialized_participant_events_and_generation_microphone_ownership(
    monkeypatch, mode,
) -> None:
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

    runtime = session_runtime.ProductionRuntimeManager(
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
            _readers: object,
            track: object,
            participant_identity: str,
            participant_sid: str,
            generation: int,
        ) -> None:
            observations.append((participant_identity, participant_sid, track))
            observation_started.set()
            reader_events.append(("started", generation))
            if mode == "generation_sync":
                try:
                    await asyncio.Event().wait()
                finally:
                    reader_events.append(("closed", generation))

        monkeypatch.setattr(
            microphone_reader.MicrophoneReaderOwner,
            "observe",
            observe_microphone,
        )
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
        coordinator = runtime._owners[session_id].coordinator

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


def test_production_stop_releases_running_microphone_reader_resources(monkeypatch) -> None:
    session_id = "20000000-0000-4000-8000-000000000010"

    async def exercise() -> None:
        stream_entered = asyncio.Event()
        stream_closed = asyncio.Event()
        monitor_started = asyncio.Event()
        monitor_finished = asyncio.Event()
        release_stream = asyncio.Event()
        disconnect_calls: list[str] = []
        deleted_sessions: list[str] = []
        deleted_rooms: list[str] = []
        cleanup_reasons: list[str] = []

        class AudioStream:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                stream_entered.set()
                await release_stream.wait()
                raise StopAsyncIteration

            async def aclose(self) -> None:
                stream_closed.set()
                release_stream.set()

        rtc = SimpleNamespace(AudioStream=AudioStream)
        monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(rtc=rtc))
        monkeypatch.setitem(sys.modules, "livekit.rtc", rtc)

        original_monitor_run = microphone_reader.MicrophoneIntegrity.run

        async def run_monitor(monitor) -> None:
            monitor_started.set()
            try:
                await original_monitor_run(monitor)
            finally:
                monitor_finished.set()

        monkeypatch.setattr(
            microphone_reader.MicrophoneIntegrity, "run", run_monitor
        )

        class Coordinator:
            generation = 0
            phase = "available"

            def is_current_participant(self, **_request: object) -> bool:
                return True

            async def cleanup(self, reason: str) -> None:
                cleanup_reasons.append(reason)
                await runtime._cleanup_owned_session(session_id)

        class Bridge:
            async def close_audio(self) -> None:
                return None

            async def receive_microphone_frame(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

        class Room:
            async def disconnect(self) -> None:
                disconnect_calls.append(session_id)

        class Sessions:
            async def delete(self, deleted_session_id: str) -> None:
                deleted_sessions.append(deleted_session_id)

        class Rooms:
            async def delete(self, room_name: str) -> None:
                deleted_rooms.append(room_name)

        async def publish_data(_payload: bytes, _topic: str) -> None:
            return None

        runtime = session_runtime.ProductionRuntimeManager(
            livekit_url="ws://127.0.0.1:7880",
            signer=object(),
            room_manager=Rooms(),
            session_repository=Sessions(),
            core_port=object(),
        )
        owner = session_runtime.ProductionSessionOwner(session_id)
        owner.room = Room()
        owner.coordinator = Coordinator()
        owner.bridge = Bridge()
        owner.publish_data = publish_data
        runtime._owners[session_id] = owner

        track = SimpleNamespace(
            sid="TR_running",
            get_stats=AsyncMock(return_value=[]),
        )
        await owner.readers.subscribe(track, "user", "participant")
        assert owner.tasks is not None
        reader_tasks = tuple(owner.tasks)
        assert len(reader_tasks) == 1
        reader_task = reader_tasks[0]
        await asyncio.wait_for(stream_entered.wait(), timeout=0.5)
        await asyncio.wait_for(monitor_started.wait(), timeout=0.5)
        assert "TR_running" in owner.readers._integrities

        await asyncio.wait_for(runtime.stop(session_id), timeout=0.5)

        assert stream_closed.is_set()
        assert monitor_finished.is_set()
        assert reader_task.done()
        assert reader_task.cancelled()
        assert owner.readers._integrities == {}
        assert runtime._owners == {}
        assert cleanup_reasons == ["explicit"]
        assert disconnect_calls == [session_id]
        assert deleted_sessions == [session_id]
        assert deleted_rooms == [f"voice-{session_id}"]

    asyncio.run(exercise())


def test_production_stop_releases_local_ownership_before_external_cleanup_finishes() -> None:
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

        runtime = runtime_shell()
        runtime._sessions = Sessions()
        runtime._room_manager = Rooms()
        runtime._owners = {
            session_id: session_owner(
                session_id,
                room=HangingRoom(),
                tasks=set(),
                audio_source=SimpleNamespace(aclose=AsyncMock()),
            )
        }

        stop_task = asyncio.create_task(runtime.stop(session_id))
        await asyncio.wait_for(disconnect_started.wait(), timeout=0.5)
        await asyncio.wait_for(delete_started.wait(), timeout=0.5)

        assert deleted_sessions == [session_id]
        owner = runtime._owners[session_id]
        # 外部cleanupの完了を待たずにlocal ownershipだけ先に切り離す。
        assert owner.room is None
        assert owner.tasks is None
        assert owner.ready is None
        assert owner.audio_source is None
        assert owner.cleanup is not None

        release_cleanup.set()
        await asyncio.wait_for(stop_task, timeout=0.5)
        assert session_id not in runtime._owners

    asyncio.run(exercise())


def test_production_room_cleanup_survives_cancelled_stop() -> None:
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

        runtime = runtime_shell()
        runtime._sessions = Sessions()
        runtime._room_manager = Rooms()
        runtime._owners = {
            session_id: session_owner(
                session_id,
                room=Room(),
                tasks=set(),
                audio_source=SimpleNamespace(aclose=AsyncMock()),
            )
        }

        stop_task = asyncio.create_task(runtime.stop(session_id))
        await asyncio.wait_for(delete_started.wait(), timeout=0.5)
        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop_task

        assert active_rooms == {f"voice-{session_id}"}
        # 呼出側のcancelでRoom削除taskを失わず、次のstopへ引き継ぐ。
        assert runtime._owners[session_id].cleanup is not None
        release_delete.set()
        await asyncio.wait_for(runtime.stop_all(), timeout=0.5)

        assert active_rooms == set()
        assert delete_calls == [f"voice-{session_id}"]
        assert runtime._owners == {}

    asyncio.run(exercise())


def test_bootstrap_timeout_leaves_room_cleanup_owned_until_it_finishes() -> None:
    bootstrap = importlib.import_module("app.livekit_transport.bootstrap")
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
        runtime = session_runtime.ProductionRuntimeManager(
            livekit_url="ws://127.0.0.1:7880",
            signer=signer,
            room_manager=rooms,
            session_repository=sessions,
            core_port=CorePort(),
        )

        async def connect(owned_session_id: str) -> None:
            runtime._owners[owned_session_id] = session_owner(
                owned_session_id, room=Room(), tasks=set()
            )

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
        owner = runtime._owners[session_id]
        # bootstrap timeout後もRoom削除の所有は失われない。
        assert owner.room is None
        assert owner.cleanup is not None
        assert active_rooms == {f"voice-{session_id}"}

        release_delete.set()
        await asyncio.wait_for(runtime.stop_all(), timeout=0.5)

        assert active_rooms == set()
        assert session_id not in runtime._owners

    asyncio.run(exercise())


def test_production_room_manager_accepts_already_deleted_room(monkeypatch) -> None:

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

    monkeypatch.setitem(sys.modules, "livekit.api", ApiModule)

    asyncio.run(
        production_sdk.ProductionRoomManager(ApiClient()).delete("voice-session")
    )


def test_production_stop_all_retries_failed_room_cleanup() -> None:
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
        runtime = runtime_shell()
        runtime._sessions = Sessions()
        runtime._room_manager = rooms
        # live資源を持たないsessionでも、失敗したRoom削除はownerへ残る。

        from app.livekit_transport.errors import RoomCleanupPendingError

        with pytest.raises(RoomCleanupPendingError, match="room deletion failed"):
            await runtime.stop(session_id)

        assert active_rooms == {f"voice-{session_id}"}
        assert runtime._owners[session_id].cleanup is not None
        await runtime.stop_all()

        assert rooms.calls == 2
        assert active_rooms == set()
        assert runtime._owners == {}

    asyncio.run(exercise())


def test_production_stop_does_not_classify_local_failure_as_room_pending() -> None:
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
        runtime = runtime_shell()
        runtime._sessions = Sessions()
        runtime._room_manager = rooms
        runtime._owners = {
            session_id: session_owner(session_id, room=Room(), tasks=set())
        }

        with pytest.raises(
            RuntimeError, match="session binding deletion failed"
        ) as raised:
            await runtime.stop(session_id)

        from app.livekit_transport.errors import RoomCleanupPendingError

        assert not isinstance(raised.value, RoomCleanupPendingError)
        owner = runtime._owners[session_id]
        assert owner.cleanup is not None
        await runtime.stop_all()
        assert runtime._sessions.calls == 2
        assert rooms.calls == 2
        assert disconnect_calls == 1
        assert runtime._owners == {}

    asyncio.run(exercise())


def test_production_stop_retries_only_failed_room_disconnect() -> None:
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
        runtime = runtime_shell()
        runtime._sessions = sessions
        runtime._room_manager = rooms
        runtime._owners = {
            session_id: session_owner(session_id, room=room, tasks=set())
        }

        with pytest.raises(RuntimeError, match="room disconnect failed"):
            await runtime.stop(session_id)

        await runtime.stop(session_id)

        assert sessions.calls == 1
        assert rooms.calls == 1
        assert room.calls == 2
        assert runtime._owners == {}

    asyncio.run(exercise())


@pytest.mark.parametrize("failure_stage", ["connect", "tts_prepare"])
def test_production_connect_failure_is_compensated_by_bootstrap_owner(
    monkeypatch, failure_stage,
) -> None:
    bootstrap = importlib.import_module("app.livekit_transport.bootstrap")
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
    runtime = session_runtime.ProductionRuntimeManager(
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
    assert runtime._owners == {}
    assert disconnected == [session_id]

    assert closed_sources == ([session_id] if failure_stage == "tts_prepare" else [])


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
    from app.livekit_transport.measurement import LiveKitMeasurementSession

    events = []
    measurement = LiveKitMeasurementSession(
        session_id="session", character_id="miori", measurement_kind="controlled_baseline",
        record=events.append, clock_ns=lambda: 9_000_000_000,
    )
    if interruption:
        measurement.bind_response(response_id="old-response", source_utterance_ids=("old-utterance",))
    bridge = microphone_bridge._ConversationCoreBridge(NoopCoreSession(), lambda task: None, measurement=measurement)
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

    bridge = microphone_bridge._ConversationCoreBridge(Core(), lambda operation: tasks.append(asyncio.create_task(operation)))
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
        session = PreparationCoreSession(fail=fail)
        tasks: set[asyncio.Task[None]] = set()

        def schedule(operation):
            task = asyncio.create_task(operation)
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        bridge = microphone_bridge._ConversationCoreBridge(session, schedule)
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
        session = PreparationCoreSession()
        tasks = []
        bridge = microphone_bridge._ConversationCoreBridge(session, lambda op: tasks.append(asyncio.create_task(op)))
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
        session = PreparationCoreSession()
        tasks = []
        bridge = microphone_bridge._ConversationCoreBridge(session, lambda op: tasks.append(asyncio.create_task(op)))
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
        session = PreparationCoreSession()
        tasks = []
        bridge = microphone_bridge._ConversationCoreBridge(session, lambda op: tasks.append(asyncio.create_task(op)))
        begin_capture(bridge, "preparation")
        bridge.receive_microphone(bytes(9600))
        assert len(tasks) == 1
        session.accepting_input = False
        await asyncio.gather(*tasks)
        assert session.preparations == 0

    asyncio.run(exercise())


def test_only_explicit_full_playback_confirmation_releases_output_wait():
    scheduled, confirmations = [], []
    class Session:
        async def confirm_playback(self, **_request):
            # 最後のprefix自体は既に進捗通知で確認済みでも、全出力確認は受ける。
            return False
    bridge = microphone_bridge._ConversationCoreBridge(
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
    records = []
    wire = []
    async def send(payload):
        wire.append(json.loads(payload))
    delivery = core_delivery._ConversationCoreDelivery(
        coordinator=SimpleNamespace(send_core=send), audio_source=SimpleNamespace(clear=lambda _: None),
        character_participant_id="40000000-0000-4000-8000-000000000010", character_id="miori",
    )
    delivery.attach_measurement(SimpleNamespace(record_response_event=lambda **row: records.append(row)))
    asyncio.run(delivery.publish(CoreEvent(type="response_cancelled",
        session_id="20000000-0000-4000-8000-000000000010", response_id="50000000-0000-4000-8000-000000000010",
        reason="barge_in", terminal_state_bounds_ns=(1_234_000, 1_234_900))))
    assert [(r["name"], r.get("timestamp")) for r in records] == [
        ("cancel_state_lower", 1_234_000), ("cancel_state_upper", 1_234_900),
        ("response_excluded", None), ("response_cancelled", None)]
    assert records[-1]["outcome"] == "excluded"
    assert records[-1]["reason_code"] == "barge_in"
    assert all("terminal_state_bounds_ns" not in event for event in wire)


@pytest.mark.parametrize("waiting_stage", ["connect", "output", "tts", "vad"])
def test_join_expiry_during_startup_does_not_restore_cleaned_resources(monkeypatch, waiting_stage):
    from uuid import uuid4

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
        pipeline_closed = []
        async def prepare_vad(bridge):
            await pause("vad")
            bridge._voice_input = SimpleNamespace(close=AsyncMock(
                side_effect=lambda: pipeline_closed.append("vad")))
        monkeypatch.setattr(microphone_bridge._ConversationCoreBridge, "prepare_audio", prepare_vad)
        inbox = core_delivery.ProductionCoreEventInbox()
        bind = Mock(wraps=inbox.bind)
        monkeypatch.setattr(inbox, "bind", bind)
        runtime = session_runtime.ProductionRuntimeManager(
            livekit_url="ws://127.0.0.1:7880",
            signer=SimpleNamespace(issue_token=token),
            room_manager=SimpleNamespace(delete=delete),
            session_repository=SimpleNamespace(delete=delete),
            core_port=inbox,
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
        coordinator = runtime._owners[session_id].coordinator
        # 90秒待たずに、実際のjoin期限処理とcleanupを通す。
        await coordinator._expire_after(0)
        release.set()
        with pytest.raises(RuntimeError, match="runtime startup ended"):
            await asyncio.wait_for(pending, 1)
        assert runtime._owners == {}
        assert closed == ([] if waiting_stage == "connect" else ["audio"])
        assert ended == (["core"] if waiting_stage in {"tts", "vad"} else [])
        assert pipeline_closed == (["vad"] if waiting_stage == "vad" else [])
        bind.assert_not_called()
        assert len(deleted) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["eof", "exception", "invalid_frame", "cancel"])
def test_microphone_reader_termination_closes_input_and_releases_monitor(monkeypatch, ending):
    """無音中のEOF・例外・不正frameでも、終了処理と入力停止を省略しない。"""
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
    monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(rtc=rtc))
    monkeypatch.setitem(sys.modules, "livekit.rtc", rtc)
    class Coordinator:
        generation = 1
        def is_current_participant(self, **kwargs):
            return True
    class Bridge:
        def close_microphone_track(self, sid, *, reader_reason):
            closed_tracks.append((sid, reader_reason))
        async def receive_microphone_frame(self, *args, **kwargs):
            pytest.fail("不正なframeをVADへ配送した")
    async def publish_data(*args):
        pass
    async def scenario():
        owner = session_owner(
            "session",
            room=object(),
            coordinator=Coordinator(),
            bridge=Bridge(),
            publish_data=publish_data,
        )
        readers = microphone_reader.MicrophoneReaderOwner(owner)
        operation = readers.observe(
            SimpleNamespace(sid="TR_first", get_stats=AsyncMock(return_value=[])),
            "user", "participant", 1,
        )
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            await operation
        expected_reason = {
            "eof": "stream_ended", "exception": "RuntimeError",
            "invalid_frame": "invalid_audio_frame", "cancel": "cancelled",
        }[ending]
        assert closed_tracks == [("TR_first", expected_reason)]
        assert stream_closed
        assert readers._integrities == {}
    asyncio.run(scenario())


@pytest.mark.parametrize("waiting_stage", ["tts", "vad"])
def test_join_deadline_starts_only_after_all_preparation(monkeypatch, waiting_stage):
    """準備中は参加期限を消費せず、準備完了後の未参加は従来どおり終了する。"""
    from uuid import uuid4

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def pause(stage):
            if stage == waiting_stage:
                entered.set()
                await release.wait()
        class Room:
            def __init__(self):
                self.local_participant = SimpleNamespace(publish_data=AsyncMock())
            def on(self, _event):
                return lambda callback: callback
            async def connect(self, *_args):
                pass
            async def disconnect(self):
                pass
        rtc = SimpleNamespace(Room=Room)
        monkeypatch.setitem(sys.modules, "livekit", SimpleNamespace(rtc=rtc))
        monkeypatch.setitem(sys.modules, "livekit.rtc", rtc)
        async def create(**_kwargs):
            await pause("tts")
            return NoopCoreSession()
        async def prepare_vad(_self):
            await pause("vad")
        monkeypatch.setattr(microphone_bridge._ConversationCoreBridge, "prepare_audio", prepare_vad)
        room_manager = SimpleNamespace(delete=AsyncMock())
        session_repository = SimpleNamespace(delete=AsyncMock())
        runtime = session_runtime.ProductionRuntimeManager(
            livekit_url="ws://127.0.0.1:7880",
            signer=SimpleNamespace(issue_token=AsyncMock(return_value="character-token")),
            room_manager=room_manager,
            session_repository=session_repository,
            core_port=SimpleNamespace(notify=lambda _payload: None),
            core_session_factory=SimpleNamespace(create=create, create_ready=create),
        )
        runtime._prepare_output_track = AsyncMock(return_value=SimpleNamespace(
            aclose=AsyncMock(), clear=lambda _response=None: None,
        ))
        session_id = str(uuid4())
        pending = asyncio.create_task(runtime.start_runtime({
            "session_id": session_id, "identity": "character", "character_id": "miori",
            "conversation_id": str(uuid4()), "core_participant_id": str(uuid4()),
            "reconnect_grace_ms": 60_000,
        }))
        await asyncio.wait_for(entered.wait(), 1)
        coordinator = runtime._owners[session_id].coordinator
        assert coordinator._deadline_task is None
        assert not runtime._owners[session_id].ready.is_set()
        release.set()
        await asyncio.wait_for(pending, 1)
        assert runtime._owners[session_id].ready.is_set()
        assert coordinator._deadline_task is not None
        await coordinator._expire_after(0)
        assert runtime._owners == {}
        room_manager.delete.assert_awaited_once()
        session_repository.delete.assert_awaited_once()
    asyncio.run(scenario())


def test_readiness_wait_has_an_individual_deadline(monkeypatch):
    monkeypatch.setattr(session_runtime, "BOOTSTRAP_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        runtime = runtime_shell()
        runtime._owners["session"] = session_owner("session")
        with pytest.raises(BootstrapTimeoutError) as caught:
            await runtime.wait_until_ready("session")
        assert caught.value.stage == "readiness"
        runtime._owners["session"].ready.set()
        await runtime.wait_until_ready("session")
    asyncio.run(scenario())


def test_vad_preparation_timeout_closes_late_pipeline(monkeypatch):
    import threading
    monkeypatch.setattr(microphone_bridge, "BOOTSTRAP_TIMEOUT_SECONDS", 0.01)
    release, entered, closed = threading.Event(), threading.Event(), threading.Event()

    def prepare():
        entered.set()
        if not release.wait(2):
            raise RuntimeError("test preparation was not released")
        return SimpleNamespace(close=closed.set)

    monkeypatch.setattr(microphone_bridge, "check_ready", lambda: None)
    monkeypatch.setattr(microphone_bridge, "VoiceInputPipeline", prepare)

    async def scenario():
        bridge = microphone_bridge._ConversationCoreBridge(NoopCoreSession(), lambda task: None)
        try:
            with pytest.raises(BootstrapTimeoutError) as caught:
                await bridge.prepare_audio()
            assert caught.value.stage == "vad"
            assert entered.is_set()
            assert bridge._voice_input is None
        finally:
            release.set()
        assert await asyncio.to_thread(closed.wait, 1)
    asyncio.run(scenario())

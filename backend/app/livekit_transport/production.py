from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID, uuid5

from app.characters.loader import load_character_card
from app.livekit_transport.bootstrap import (
    BOOTSTRAP_TIMEOUT_SECONDS,
    BootstrapService,
    CharacterConversationBindingValidator,
    InMemorySessionBindingRepository,
    _RoomCleanupPendingError,
)
from app.livekit_transport.coordinator import (
    PRIVATE_TOPIC,
    ProductionSessionCoordinator,
    SessionCoordinatorDependencies,
)
from app.livekit_transport.delivery import CoreNotificationPort
from app.livekit_transport.runtime import MicrophoneTrackObserver, PcmFixturePublisher
from app.livekit_transport.token import LiveKitTokenSigner


LIVEKIT_URL_ENV = "LIVEKIT_URL"
LIVEKIT_API_KEY_ENV = "LIVEKIT_API_KEY"
LIVEKIT_API_SECRET_ENV = "LIVEKIT_API_SECRET"
PCM_SAMPLE_RATE = 48_000
PCM_CHANNELS = 1
PCM_FIXTURE_SAMPLES = 4_800
logger = logging.getLogger(__name__)


def _fixture_response_id(session_id: str, generation: int) -> str:
    return str(uuid5(UUID(session_id), f"fixture:{generation}"))


class LiveKitConfigurationError(RuntimeError):
    pass


def resolve_livekit_settings() -> tuple[str, str, str] | None:
    values = tuple(
        os.environ.get(name)
        for name in (LIVEKIT_URL_ENV, LIVEKIT_API_KEY_ENV, LIVEKIT_API_SECRET_ENV)
    )
    if values == (None, None, None):
        return None
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise LiveKitConfigurationError("LiveKit configuration is incomplete")
    return values  # type: ignore[return-value]


class ProductionTokenSigner:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self._signer = LiveKitTokenSigner(api_key=api_key, api_secret=api_secret)

    async def issue(self, **request: object) -> str:
        return await self._signer.issue(**request)  # type: ignore[arg-type]

    async def issue_token(self, request: dict[str, object]) -> str:
        return await self.issue(
            identity=str(request["identity"]),
            room=str(request["room"]),
            ttl_seconds=int(request["ttl_seconds"]),
            grant={
                "room_join": True,
                "can_subscribe": bool(request["can_subscribe"]),
                "can_publish": bool(request["can_publish"]),
                "can_publish_data": bool(request["can_publish_data"]),
                "can_publish_sources": list(request["can_publish_sources"]),
            },
        )


class ProductionRoomManager:
    def __init__(self, livekit_api: object) -> None:
        self._api = livekit_api

    async def create(self, room_name: str) -> None:
        from livekit import api

        await self._api.room.create_room(api.CreateRoomRequest(name=room_name))

    async def delete(self, room_name: str) -> None:
        from livekit import api

        await self._api.room.delete_room(api.DeleteRoomRequest(room=room_name))

class _ObservationPublisher:
    def __init__(
        self,
        publish: Callable[[bytes, str], Awaitable[None]],
        generation: Callable[[], int],
        schedule: Callable[[Awaitable[None]], None],
    ) -> None:
        self._publish = publish
        self._generation = generation
        self._schedule = schedule

    def record(self, observation: dict[str, object]) -> None:
        frame = {
            "protocol_version": "1.0",
            "type": "microphone_observation",
            "generation": self._generation(),
            **observation,
        }
        self._schedule(
            self._publish(json.dumps(frame, separators=(",", ":")).encode(), PRIVATE_TOPIC)
        )


class ProductionCoreEventInbox:
    """Conversation Core が同一process内で消費する検証済みevent入口。"""

    def __init__(self) -> None:
        self._events: asyncio.Queue[bytes] = asyncio.Queue()

    def notify(self, payload: bytes) -> None:
        self._events.put_nowait(payload)

    async def receive(self) -> bytes:
        return await self._events.get()


class _RtcAudioSource(Protocol):
    async def capture_frame(self, frame: object) -> None: ...


class _LiveKitPcmAudioSource:
    def __init__(self, source: _RtcAudioSource) -> None:
        self._source = source

    async def publish(self, pcm: bytes) -> None:
        from livekit import rtc

        samples_per_channel = len(pcm) // (2 * PCM_CHANNELS)
        frame = rtc.AudioFrame(
            pcm,
            PCM_SAMPLE_RATE,
            PCM_CHANNELS,
            samples_per_channel,
        )
        await self._source.capture_frame(frame)


class _PrivateSegmentMetadataPublisher:
    def __init__(
        self, publish_data: Callable[[bytes, str], Awaitable[None]]
    ) -> None:
        self._publish_data = publish_data

    async def publish(self, metadata: dict[str, object]) -> None:
        frame = {"protocol_version": "1.0", **metadata}
        await self._publish_data(
            json.dumps(frame, separators=(",", ":")).encode(), PRIVATE_TOPIC
        )


def _deterministic_pcm_fixture() -> bytes:
    return b"".join(
        (12_000 if (index // 120) % 2 == 0 else -12_000).to_bytes(
            2, "little", signed=True
        )
        for index in range(PCM_FIXTURE_SAMPLES)
    )


@dataclass
class _SessionCleanupState:
    room: object | None
    pending_runtime_tasks: list[asyncio.Task[None]]
    session_binding_deleted: bool = False
    room_deleted: bool = False
    room_cleanup_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ProductionRuntimeManager:
    manages_owned_cleanup = True

    def __init__(
        self,
        *,
        livekit_url: str,
        signer: ProductionTokenSigner,
        room_manager: ProductionRoomManager,
        session_repository: InMemorySessionBindingRepository,
        core_port: CoreNotificationPort,
    ) -> None:
        self._livekit_url = livekit_url
        self._signer = signer
        self._room_manager = room_manager
        self._sessions = session_repository
        self._core_port = core_port
        self._rooms: dict[str, object] = {}
        self._coordinators: dict[str, ProductionSessionCoordinator] = {}
        self._session_tasks: dict[str, set[asyncio.Task[None]]] = {}
        self._ready: dict[str, asyncio.Event] = {}
        self._fixture_sources: dict[str, _LiveKitPcmAudioSource] = {}
        self._fixture_generations: dict[str, int] = {}
        self._fixture_locks: dict[str, asyncio.Lock] = {}
        self._cleanup_states: dict[str, _SessionCleanupState] = {}

    async def connect(self, session_id: str) -> None:
        reservation = self._sessions.get(session_id)
        if reservation is None:
            raise RuntimeError("session reservation is required")
        await self.start_runtime(
            {
                "session_id": session_id,
                "identity": f"character-{reservation.request['character_id']}-{session_id}",
                "core_participant_id": str(reservation.request.get("participant_id", session_id)),
                "reconnect_grace_ms": min(
                    int(reservation.request["requested_reconnect_grace_ms"]), 60_000
                ),
            }
        )

    async def wait_until_ready(self, session_id: str) -> None:
        await self._ready[session_id].wait()

    async def start_runtime(self, request: dict[str, object]) -> None:
        from livekit import rtc

        session_id = str(request["session_id"])
        room_name = f"voice-{session_id}"
        user_identity = f"user-{session_id}"
        token = await self._signer.issue_token(
            {
                "identity": request["identity"],
                "room": room_name,
                "ttl_seconds": 90,
                "can_subscribe": True,
                "can_publish": True,
                "can_publish_data": True,
                "can_publish_sources": ["microphone"],
            }
        )
        room = rtc.Room()

        async def publish_data(payload: bytes, topic: str) -> None:
            await room.local_participant.publish_data(payload, reliable=True, topic=topic)

        async def cleanup(_owned_session_id: str) -> None:
            await self._cleanup_owned_session(session_id)

        async def generation_ready() -> None:
            await self._publish_fixture(session_id, coordinator, publish_data)

        coordinator = ProductionSessionCoordinator(
            session_id=session_id,
            user_identity=user_identity,
            core_participant_id=str(request["core_participant_id"]),
            reconnect_grace_ms=int(request["reconnect_grace_ms"]),
            dependencies=SessionCoordinatorDependencies(
                publish_data=publish_data,
                cleanup=cleanup,
                generation_ready=generation_ready,
            ),
            core_port=self._core_port,
        )
        self._rooms[session_id] = room
        self._coordinators[session_id] = coordinator
        self._session_tasks[session_id] = set()
        self._ready[session_id] = asyncio.Event()
        self._fixture_locks[session_id] = asyncio.Lock()

        @room.on("participant_connected")
        def participant_connected(participant: object) -> None:
            reconnected = coordinator.participant_connected(
                identity=str(participant.identity),
                participant_sid=str(participant.sid),
                room_sid=str(room.sid),
            )

            async def handle_connected() -> None:
                if reconnected:
                    await coordinator.synchronize_reconnection()
                if str(participant.identity) == user_identity:
                    await self._ready[session_id].wait()
                    await self._publish_fixture(session_id, coordinator, publish_data)

            self._schedule_task(session_id, handle_connected())

        @room.on("participant_disconnected")
        def participant_disconnected(participant: object) -> None:
            coordinator.participant_disconnected(
                identity=str(participant.identity), participant_sid=str(participant.sid)
            )

        @room.on("data_received")
        def data_received(packet: object) -> None:
            participant = getattr(packet, "participant", None)
            if participant is None:
                return
            self._schedule_task(
                session_id,
                coordinator.receive_data(
                    identity=str(participant.identity),
                    participant_sid=str(participant.sid),
                    topic=str(packet.topic),
                    payload=bytes(packet.data),
                ),
            )

        @room.on("track_subscribed")
        def track_subscribed(track: object, publication: object, participant: object) -> None:
            if (
                publication.source != rtc.TrackSource.SOURCE_MICROPHONE
                or not coordinator.is_current_participant(
                    identity=str(participant.identity),
                    participant_sid=str(participant.sid),
                )
            ):
                return
            self._schedule_task(
                session_id,
                self._observe_microphone(
                    session_id,
                    track,
                    coordinator,
                    str(participant.identity),
                    str(participant.sid),
                    coordinator.generation,
                    publish_data,
                ),
            )

        await room.connect(self._livekit_url, token)
        coordinator.start_join_deadline()
        await self._prepare_fixture_track(session_id, room)
        self._ready[session_id].set()

    async def _observe_microphone(
        self,
        session_id: str,
        track: object,
        coordinator: ProductionSessionCoordinator,
        participant_identity: str,
        participant_sid: str,
        generation: int,
        publish_data: Callable[[bytes, str], Awaitable[None]],
    ) -> None:
        from livekit import rtc

        observer = MicrophoneTrackObserver(
            observation_port=_ObservationPublisher(
                publish_data,
                lambda: coordinator.generation,
                lambda coroutine: self._schedule_task(session_id, coroutine),
            )
        )
        stream = rtc.AudioStream(track, sample_rate=PCM_SAMPLE_RATE, num_channels=1)
        try:
            async for event in stream:
                if (
                    coordinator.generation != generation
                    or not coordinator.is_current_participant(
                        identity=participant_identity,
                        participant_sid=participant_sid,
                    )
                ):
                    return
                frame = event.frame
                observer.receive_frame(
                    pcm=bytes(frame.data),
                    sample_count=int(frame.samples_per_channel),
                    received_at_ms=int(time.monotonic() * 1000),
                )
                if session_id not in self._rooms:
                    return
        finally:
            await stream.aclose()

    def _schedule_task(
        self, session_id: str, coroutine: Awaitable[None]
    ) -> None:
        tasks = self._session_tasks.get(session_id)
        if tasks is None:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            elif isinstance(coroutine, asyncio.Future):
                coroutine.cancel()
            return
        task = asyncio.create_task(self._run_owned_task(session_id, coroutine))
        tasks.add(task)
        task.add_done_callback(lambda completed: self._task_done(tasks, completed))

    async def _run_owned_task(
        self, session_id: str, operation: Awaitable[None]
    ) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception:
            coordinator = self._coordinators.get(session_id)
            if coordinator is not None and coordinator.phase != "ended":
                await coordinator.cleanup("runtime_error")
            raise

    @staticmethod
    def _task_done(
        tasks: set[asyncio.Task[None]], completed: asyncio.Task[None]
    ) -> None:
        tasks.discard(completed)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.error(
                "LiveKit session task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _prepare_fixture_track(self, session_id: str, room: object) -> None:
        from livekit import rtc

        source = rtc.AudioSource(PCM_SAMPLE_RATE, PCM_CHANNELS)
        track = rtc.LocalAudioTrack.create_audio_track("character-fixture", source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await room.local_participant.publish_track(track, options)
        self._fixture_sources[session_id] = _LiveKitPcmAudioSource(source)

    async def _publish_fixture(
        self,
        session_id: str,
        coordinator: ProductionSessionCoordinator,
        publish_data: Callable[[bytes, str], Awaitable[None]],
    ) -> None:
        async with self._fixture_locks[session_id]:
            generation = coordinator.generation
            if self._fixture_generations.get(session_id) == generation:
                return
            response_id = _fixture_response_id(session_id, generation)
            coordinator.begin_response(response_id=response_id)
            publisher = PcmFixturePublisher(
                audio_source=self._fixture_sources[session_id],
                metadata_port=_PrivateSegmentMetadataPublisher(publish_data),
            )
            await publisher.publish(
                response_id=response_id,
                audio_sequence=0,
                generation=generation,
                pcm=_deterministic_pcm_fixture(),
                sample_count=PCM_FIXTURE_SAMPLES,
            )
            self._fixture_generations[session_id] = generation

    async def send_core(self, session_id: str, payload: bytes) -> None:
        coordinator = self._coordinators.get(session_id)
        if coordinator is None:
            raise RuntimeError("LiveKit session is not active")
        await coordinator.send_core(payload)

    async def stop(self, session_id: str) -> None:
        coordinator = self._coordinators.get(session_id)
        if coordinator is not None:
            await coordinator.cleanup("explicit")
        else:
            await self._cleanup_owned_session(session_id)

    async def stop_all(self) -> None:
        session_ids = set(self._rooms) | set(self._cleanup_states)
        for session_id in session_ids:
            await self.stop(session_id)

    def _release_runtime_ownership(
        self, session_id: str
    ) -> tuple[object | None, list[asyncio.Task[None]]]:
        self._coordinators.pop(session_id, None)
        tasks = self._session_tasks.pop(session_id, set())
        pending_tasks: list[asyncio.Task[None]] = []
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
                pending_tasks.append(task)
        room = self._rooms.pop(session_id, None)
        self._ready.pop(session_id, None)
        self._fixture_sources.pop(session_id, None)
        self._fixture_generations.pop(session_id, None)
        self._fixture_locks.pop(session_id, None)
        return room, pending_tasks

    async def _cleanup_owned_session(self, session_id: str) -> None:
        state = self._cleanup_states.get(session_id)
        if state is None:
            room, pending_tasks = self._release_runtime_ownership(session_id)
            state = _SessionCleanupState(
                room=room,
                pending_runtime_tasks=pending_tasks,
            )
            self._cleanup_states[session_id] = state

        async with state.lock:
            room_cleanup_task = self._start_room_cleanup(session_id, state)
            local_operations: list[Awaitable[object]] = []
            if not state.session_binding_deleted:
                local_operations.append(
                    asyncio.create_task(self._delete_session_binding(session_id, state))
                )
            if state.room is not None:
                local_operations.append(
                    asyncio.create_task(self._disconnect_room(state))
                )
            if state.pending_runtime_tasks:
                local_operations.append(
                    asyncio.create_task(self._finish_runtime_tasks(state))
                )

            if room_cleanup_task is None:
                local_results = await asyncio.gather(
                    *local_operations, return_exceptions=True
                )
                room_cleanup_result: object = None
            else:
                local_results, room_cleanup_result = await asyncio.gather(
                    asyncio.gather(*local_operations, return_exceptions=True),
                    asyncio.shield(room_cleanup_task),
                    return_exceptions=True,
                )
            for result in local_results:
                if isinstance(result, BaseException):
                    raise result
            if isinstance(room_cleanup_result, BaseException):
                raise _RoomCleanupPendingError(
                    str(room_cleanup_result)
                ) from room_cleanup_result
            if self._cleanup_states.get(session_id) is state:
                self._cleanup_states.pop(session_id)

    async def _delete_session_binding(
        self, session_id: str, state: _SessionCleanupState
    ) -> None:
        await self._sessions.delete(session_id)
        state.session_binding_deleted = True

    @staticmethod
    async def _disconnect_room(state: _SessionCleanupState) -> None:
        if state.room is None:
            raise RuntimeError("cleanup room is required")
        await state.room.disconnect()
        state.room = None

    @staticmethod
    async def _finish_runtime_tasks(state: _SessionCleanupState) -> None:
        await asyncio.gather(*state.pending_runtime_tasks, return_exceptions=True)
        state.pending_runtime_tasks.clear()

    def _start_room_cleanup(
        self, session_id: str, state: _SessionCleanupState
    ) -> asyncio.Task[None] | None:
        if state.room_deleted:
            return None
        existing = state.room_cleanup_task
        if existing is not None and not existing.done():
            return existing
        if existing is not None and not existing.cancelled():
            existing.exception()
        task = asyncio.create_task(self._delete_owned_room(session_id, state))
        state.room_cleanup_task = task
        task.add_done_callback(self._consume_room_cleanup_result)
        return task

    async def _delete_owned_room(
        self, session_id: str, state: _SessionCleanupState
    ) -> None:
        await self._room_manager.delete(f"voice-{session_id}")
        state.room_deleted = True

    @staticmethod
    def _consume_room_cleanup_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()


async def configure_production_resources(app: object) -> object | None:
    settings = resolve_livekit_settings()
    if settings is None:
        return None
    livekit_url, api_key, api_secret = settings
    from livekit import api

    livekit_api = api.LiveKitAPI(livekit_url, api_key, api_secret)
    room_manager = ProductionRoomManager(livekit_api)
    signer = ProductionTokenSigner(api_key, api_secret)
    sessions = InMemorySessionBindingRepository()
    core_events = ProductionCoreEventInbox()
    runtime = ProductionRuntimeManager(
        livekit_url=livekit_url,
        signer=signer,
        room_manager=room_manager,
        session_repository=sessions,
        core_port=core_events,
    )
    validator = CharacterConversationBindingValidator(
        character_loader=load_character_card,
        conversations=app.state.conversation_history_repository,
    )
    bootstrap = BootstrapService(
        session_repository=sessions,
        room_manager=room_manager,
        runtime_manager=runtime,
        token_signer=signer,
        timeout_seconds=BOOTSTRAP_TIMEOUT_SECONDS,
        binding_validator=validator,
    )
    app.state.livekit_room_manager = room_manager
    app.state.livekit_session_repository = sessions
    app.state.livekit_runtime_manager = runtime
    app.state.livekit_token_signer = signer
    app.state.livekit_bootstrap_service = bootstrap
    app.state.livekit_core_events = core_events
    app.state.livekit_url = livekit_url
    return livekit_api

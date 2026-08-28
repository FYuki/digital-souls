from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from fastapi import FastAPI

from app.characters.loader import load_character_card
from app.characters.loader import load_tts_config
from app.conversation_core import ConversationCoreSession, CoreEvent, StageObservation
from app.conversation_core.adapters import (
    ConversationHistoryPersistenceAdapter,
    PromptLlmAdapter,
    VoicevoxTtsAdapter,
    WhisperSttAdapter,
)
from app.livekit_transport.bootstrap import (
    BOOTSTRAP_TIMEOUT_SECONDS,
    BootstrapService,
    CharacterConversationBindingValidator,
    InMemorySessionBindingRepository,
)
from app.livekit_transport.coordinator import (
    PRIVATE_TOPIC,
    ProductionSessionCoordinator,
    SessionCoordinatorDependencies,
)
from app.livekit_transport.delivery import CoreNotificationPort
from app.livekit_transport.errors import RoomCleanupPendingError
from app.livekit_transport.runtime import MicrophoneTrackObserver
from app.livekit_transport.token import IssuedToken, LiveKitTokenSigner

if TYPE_CHECKING:
    import livekit.api as livekit_api
    import livekit.rtc as rtc


LIVEKIT_URL_ENV = "LIVEKIT_URL"
LIVEKIT_API_KEY_ENV = "LIVEKIT_API_KEY"
LIVEKIT_API_SECRET_ENV = "LIVEKIT_API_SECRET"
PCM_SAMPLE_RATE = 48_000
STT_SAMPLE_RATE = 16_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH_BYTES = 2
logger = logging.getLogger(__name__)


def _livekit_rtc_module() -> Any:
    return importlib.import_module("livekit.rtc")


def _livekit_api_module() -> Any:
    return importlib.import_module("livekit.api")


def _required_int(value: object, field: str) -> int:
    if not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return value


def _required_string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{field} must be a list of strings")
    return value


class LiveKitConfigurationError(RuntimeError):
    pass


def resolve_livekit_settings() -> tuple[str, str, str] | None:
    livekit_url = os.environ.get(LIVEKIT_URL_ENV)
    api_key = os.environ.get(LIVEKIT_API_KEY_ENV)
    api_secret = os.environ.get(LIVEKIT_API_SECRET_ENV)
    if livekit_url is None and api_key is None and api_secret is None:
        return None
    if (
        livekit_url is None
        or not livekit_url.strip()
        or api_key is None
        or not api_key.strip()
        or api_secret is None
        or not api_secret.strip()
    ):
        raise LiveKitConfigurationError("LiveKit configuration is incomplete")
    return livekit_url, api_key, api_secret


class ProductionTokenSigner:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self._signer = LiveKitTokenSigner(api_key=api_key, api_secret=api_secret)

    async def issue(self, **request: object) -> str:
        return await self._signer.issue(**request)  # type: ignore[arg-type]

    async def issue_with_expiration(self, **request: object) -> IssuedToken:
        return await self._signer.issue_with_expiration(**request)  # type: ignore[arg-type]

    async def issue_token(self, request: dict[str, object]) -> str:
        return await self.issue(
            identity=str(request["identity"]),
            room=str(request["room"]),
            ttl_seconds=_required_int(request["ttl_seconds"], "ttl_seconds"),
            grant={
                "room_join": True,
                "can_subscribe": bool(request["can_subscribe"]),
                "can_publish": bool(request["can_publish"]),
                "can_publish_data": bool(request["can_publish_data"]),
                "can_publish_sources": _required_string_list(
                    request["can_publish_sources"], "can_publish_sources"
                ),
            },
        )


class ProductionRoomManager:
    def __init__(self, livekit_api: livekit_api.LiveKitAPI) -> None:
        self._api = livekit_api

    async def create(self, room_name: str) -> None:
        api = _livekit_api_module()

        await self._api.room.create_room(api.CreateRoomRequest(name=room_name))

    async def delete(self, room_name: str) -> None:
        api = _livekit_api_module()

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
        self._handlers: dict[str, Callable[[bytes], None]] = {}
        self._pending_by_session: dict[str, deque[bytes]] = {}

    def notify(self, payload: bytes) -> None:
        value = json.loads(payload)
        session_id = value.get("session_id") if isinstance(value, dict) else None
        if isinstance(session_id, str):
            handler = self._handlers.get(session_id)
            if handler is not None:
                handler(payload)
                return
            self._pending_by_session.setdefault(session_id, deque()).append(payload)

    def bind(self, session_id: str, handler: Callable[[bytes], None]) -> None:
        if session_id in self._handlers:
            raise RuntimeError("Core event handler is already bound")
        self._handlers[session_id] = handler
        pending = self._pending_by_session.get(session_id)
        while pending:
            handler(pending.popleft())
        self._pending_by_session.pop(session_id, None)

    def unbind(self, session_id: str) -> None:
        self._handlers.pop(session_id, None)
        self._pending_by_session.pop(session_id, None)


class _RtcAudioSource(Protocol):
    async def capture_frame(self, frame: rtc.AudioFrame) -> None: ...


class _LiveKitPcmAudioSource:
    def __init__(self, source: _RtcAudioSource) -> None:
        self._source = source

    async def publish(self, pcm: bytes) -> None:
        rtc_module = _livekit_rtc_module()

        samples_per_channel = len(pcm) // (
            PCM_SAMPLE_WIDTH_BYTES * PCM_CHANNELS
        )
        frame: rtc.AudioFrame = rtc_module.AudioFrame(
            pcm,
            PCM_SAMPLE_RATE,
            PCM_CHANNELS,
            samples_per_channel,
        )
        await self._source.capture_frame(frame)


class _HistoryService(Protocol):
    def open_session(self, character_id: str, conversation_id: UUID) -> object: ...


class _CoreSessionFactory(Protocol):
    def create(
        self,
        *,
        session_id: str,
        character_id: str,
        conversation_id: UUID,
        delivery: object,
    ) -> ConversationCoreSession: ...


class _MissingCoreSessionFactory:
    def create(self, **_request: object) -> ConversationCoreSession:
        raise RuntimeError("Conversation Core session factory is required")


class _LoggingCoreObservation:
    async def record(self, observation: StageObservation) -> None:
        logger.info(
            "Conversation Core stage: session_id=%s response_id=%s generation=%s stage=%s outcome=%s utterance_id=%s",
            observation.session_id,
            observation.response_id,
            observation.generation,
            observation.stage,
            observation.outcome,
            observation.utterance_id,
        )


class ProductionConversationCoreSessionFactory:
    def __init__(
        self,
        *,
        transcriber: object,
        synthesizer: object,
        history_service: _HistoryService,
        generate_reply: Callable[[str, object, str], str],
    ) -> None:
        self._transcriber = transcriber
        self._synthesizer = synthesizer
        self._history_service = history_service
        self._generate_reply = generate_reply

    def create(
        self,
        *,
        session_id: str,
        character_id: str,
        conversation_id: UUID,
        delivery: object,
    ) -> ConversationCoreSession:
        history_session = self._history_service.open_session(
            character_id, conversation_id
        )
        speaker_id = load_tts_config(character_id).speaker_id
        return ConversationCoreSession(
            session_id=session_id,
            response_id_factory=lambda: str(uuid4()),
            delivery=delivery,  # type: ignore[arg-type]
            persistence=ConversationHistoryPersistenceAdapter(
                history_session=history_session  # type: ignore[arg-type]
            ),
            observation=_LoggingCoreObservation(),
            stt=WhisperSttAdapter(transcriber=self._transcriber),  # type: ignore[arg-type]
            llm=PromptLlmAdapter(
                generate_reply=lambda transcript: self._generate_reply(
                    character_id, history_session, transcript
                )
            ),
            tts=VoicevoxTtsAdapter(
                client=self._synthesizer,  # type: ignore[arg-type]
                output_sample_rate=PCM_SAMPLE_RATE,
                output_channels=PCM_CHANNELS,
                output_sample_width=PCM_SAMPLE_WIDTH_BYTES,
                speaker_id=speaker_id,
            ),
        )


class _ConversationCoreDelivery:
    def __init__(
        self,
        *,
        coordinator: ProductionSessionCoordinator,
        audio_source: _LiveKitPcmAudioSource,
        character_participant_id: str,
        character_id: str,
    ) -> None:
        self._coordinator = coordinator
        self._audio_source = audio_source
        self._character_speaker = {
            "participant_id": character_participant_id,
            "role": "character",
            "character_id": character_id,
        }

    async def publish(self, event: CoreEvent) -> None:
        if event.type == "response_started":
            if event.response_id is None:
                raise ValueError("response_started requires response_id")
            self._coordinator.begin_response(response_id=event.response_id)
        if event.type == "response_audio_segment":
            if event.audio is None:
                raise ValueError("audio event requires PCM bytes")
            await self._coordinator.send_core(self._voice_payload(event))
            await self._audio_source.publish(event.audio)
            return
        if event.type == "response_privacy_skipped":
            if event.source_utterance_ids is None:
                raise ValueError("privacy event requires source utterance ids")
            for utterance_id in event.source_utterance_ids:
                await self._coordinator.send_core(
                    self._voice_payload(event, utterance_id=utterance_id)
                )
            return
        await self._coordinator.send_core(self._voice_payload(event))

    def _voice_payload(
        self, event: CoreEvent, *, utterance_id: str | None = None
    ) -> bytes:
        payload: dict[str, object] = {
            "protocol_version": "1.0",
            "event_id": str(uuid4()),
            "session_id": event.session_id,
            "monotonic_timestamp_ms": int(time.monotonic() * 1000),
        }
        if event.type == "response_started":
            if event.source_utterance_ids is None:
                raise ValueError("response_started requires source utterance ids")
            payload.update(
                type=event.type,
                response_id=event.response_id,
                speaker=self._character_speaker,
                source_utterance_ids=list(event.source_utterance_ids),
            )
        elif event.type == "response_delta":
            if event.text_range is None:
                raise ValueError("response_delta requires text range")
            payload.update(
                type=event.type,
                response_id=event.response_id,
                text_sequence=event.text_sequence,
                text=event.text,
                text_range={
                    "start": event.text_range[0],
                    "end": event.text_range[1],
                },
            )
        elif event.type == "response_audio_segment":
            if event.text_range is None:
                raise ValueError("response_audio_segment requires text range")
            payload.update(
                type=event.type,
                response_id=event.response_id,
                audio_sequence=event.audio_sequence,
                text_range={
                    "start": event.text_range[0],
                    "end": event.text_range[1],
                },
            )
        elif event.type == "response_completed":
            payload.update(
                type=event.type,
                response_id=event.response_id,
                last_text_sequence=event.last_text_sequence,
                last_audio_sequence=event.last_audio_sequence,
            )
        elif event.type == "response_cancelled":
            payload.update(type=event.type, response_id=event.response_id, reason=event.reason)
        elif event.type == "response_failed":
            error_code = (
                event.reason
                if event.reason is not None
                else "conversation_core_failed"
            )
            payload.update(
                type=event.type,
                response_id=event.response_id,
                error_code=error_code,
                recoverable=True,
            )
        elif event.type == "response_privacy_skipped":
            payload.update(
                type="utterance_discarded",
                utterance_id=utterance_id,
                reason="privacy",
            )
        else:
            raise ValueError(f"unsupported Core event type: {event.type}")
        return json.dumps(payload, separators=(",", ":")).encode()


@dataclass
class _UserAudioCapture:
    utterance_id: str
    pcm: bytearray = field(default_factory=bytearray)
    finalized: bool = False


class _ConversationCoreBridge:
    def __init__(
        self,
        session: ConversationCoreSession,
        schedule: Callable[[Awaitable[None]], None],
    ) -> None:
        self._session = session
        self._schedule = schedule
        self._user_audio_captures: deque[_UserAudioCapture] = deque()

    def notify(self, payload: bytes) -> None:
        event = json.loads(payload)
        if event["type"] == "speech_started" and self._is_user_event(event):
            self._user_audio_captures.append(
                _UserAudioCapture(utterance_id=str(event["utterance_id"]))
            )
            return
        if event["type"] == "utterance_finalized" and self._is_user_event(event):
            utterance_id = str(event["utterance_id"])
            for capture in self._user_audio_captures:
                if capture.utterance_id == utterance_id:
                    capture.finalized = True
                    self._finalize_user_audio_if_ready()
                    break
            return
        self._schedule(self._receive(event))

    def receive_microphone(self, pcm: bytes) -> None:
        if not self._user_audio_captures:
            return
        capture = self._user_audio_captures[0]
        capture.pcm.extend(pcm)
        self._finalize_user_audio_if_ready()

    def _finalize_user_audio_if_ready(self) -> None:
        if not self._user_audio_captures:
            return
        capture = self._user_audio_captures[0]
        if not capture.finalized or not capture.pcm:
            return
        utterance_id = capture.utterance_id
        microphone_pcm = bytes(capture.pcm)
        self._user_audio_captures.popleft()
        self._schedule(
            self._finalize_user_audio(
                utterance_id=utterance_id,
                microphone_pcm=microphone_pcm,
            )
        )

    async def _receive(self, event: dict[str, object]) -> None:
        event_type = event["type"]
        if event_type == "response_cancel_requested":
            await self._session.cancel_response(
                response_id=str(event["response_id"]),
                reason=str(event["reason"]),
            )
        elif event_type in ("playback_completed", "playback_stopped"):
            await self._session.confirm_playback(
                response_id=str(event["response_id"]),
                last_played_audio_sequence=_required_int(
                    event["last_played_audio_sequence"],
                    "last_played_audio_sequence",
                ),
            )
        elif event_type == "session_disconnected":
            await self._session.disconnect()
        elif event_type == "session_reconnected":
            await self._session.reconnect()

    async def _finalize_user_audio(
        self, *, utterance_id: str, microphone_pcm: bytes
    ) -> None:
        task = self._session.start_transcription(
            utterance_id=utterance_id,
            audio=microphone_pcm,
            should_response=True,
        )
        task.add_done_callback(self._consume_task)

    @staticmethod
    def _is_user_event(event: dict[str, object]) -> bool:
        speaker = event.get("speaker")
        return isinstance(speaker, dict) and speaker.get("role") == "user"

    async def end(self) -> None:
        await self._session.end()

    @staticmethod
    def _consume_task(task: asyncio.Task[object]) -> None:
        if not task.cancelled():
            task.exception()


@dataclass
class _SessionCleanupState:
    room: rtc.Room | None
    pending_runtime_tasks: list[asyncio.Task[None]]
    session_binding_deleted: bool = False
    room_deleted: bool = False
    room_cleanup_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ProductionRuntimeManager:
    manages_owned_cleanup: bool = True

    def __init__(
        self,
        *,
        livekit_url: str,
        signer: ProductionTokenSigner,
        room_manager: ProductionRoomManager,
        session_repository: InMemorySessionBindingRepository,
        core_port: CoreNotificationPort,
        core_session_factory: _CoreSessionFactory | None = None,
    ) -> None:
        self._livekit_url = livekit_url
        self._signer = signer
        self._room_manager = room_manager
        self._sessions = session_repository
        self._core_port = core_port
        self._core_session_factory = (
            core_session_factory
            if core_session_factory is not None
            else _MissingCoreSessionFactory()
        )
        self._rooms: dict[str, rtc.Room] = {}
        self._coordinators: dict[str, ProductionSessionCoordinator] = {}
        self._session_tasks: dict[str, set[asyncio.Task[None]]] = {}
        self._participant_event_tails: dict[str, asyncio.Task[None]] = {}
        self._ready: dict[str, asyncio.Event] = {}
        self._audio_sources: dict[str, _LiveKitPcmAudioSource] = {}
        self._core_sessions: dict[str, ConversationCoreSession] = {}
        self._core_bridges: dict[str, _ConversationCoreBridge] = {}
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
                "character_id": str(reservation.request["character_id"]),
                "conversation_id": str(reservation.request["conversation_id"]),
                "reconnect_grace_ms": min(
                    _required_int(
                        reservation.request["requested_reconnect_grace_ms"],
                        "requested_reconnect_grace_ms",
                    ),
                    60_000,
                ),
            }
        )

    async def wait_until_ready(self, session_id: str) -> None:
        await self._ready[session_id].wait()

    async def start_runtime(self, request: dict[str, object]) -> None:
        rtc_module = _livekit_rtc_module()

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
        room: rtc.Room = rtc_module.Room()

        async def publish_data(payload: bytes, topic: str) -> None:
            await room.local_participant.publish_data(payload, reliable=True, topic=topic)

        async def cleanup(_owned_session_id: str) -> None:
            await self._cleanup_owned_session(session_id)

        async def generation_ready() -> None:
            return None

        coordinator = ProductionSessionCoordinator(
            session_id=session_id,
            user_identity=user_identity,
            core_participant_id=str(request["core_participant_id"]),
            reconnect_grace_ms=_required_int(
                request["reconnect_grace_ms"], "reconnect_grace_ms"
            ),
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

        def participant_connected(participant: rtc.RemoteParticipant) -> None:
            async def handle_connected() -> None:
                room_sid_value = room.sid
                room_sid = (
                    str(await room_sid_value)
                    if inspect.isawaitable(room_sid_value)
                    else str(room_sid_value)
                )
                reconnected = coordinator.participant_connected(
                    identity=str(participant.identity),
                    participant_sid=str(participant.sid),
                    room_sid=room_sid,
                )
                if reconnected:
                    await coordinator.synchronize_reconnection()

            self._schedule_serialized_participant_operation(
                session_id, handle_connected
            )
        room.on("participant_connected")(participant_connected)

        def participant_disconnected(participant: rtc.RemoteParticipant) -> None:
            async def handle_disconnected() -> None:
                coordinator.participant_disconnected(
                    identity=str(participant.identity),
                    participant_sid=str(participant.sid),
                )

            self._schedule_serialized_participant_operation(
                session_id, handle_disconnected
            )

        room.on("participant_disconnected")(participant_disconnected)

        def data_received(packet: rtc.DataPacket) -> None:
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

        room.on("data_received")(data_received)

        def track_subscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            async def handle_track_subscribed() -> None:
                if (
                    publication.source
                    != rtc_module.TrackSource.SOURCE_MICROPHONE
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

            self._schedule_serialized_participant_operation(
                session_id, handle_track_subscribed
            )

        room.on("track_subscribed")(track_subscribed)

        await room.connect(self._livekit_url, token)
        coordinator.start_join_deadline()
        audio_source = await self._prepare_output_track(room)
        delivery = _ConversationCoreDelivery(
            coordinator=coordinator,
            audio_source=audio_source,
            character_participant_id=str(uuid4()),
            character_id=str(request["character_id"]),
        )
        core_session = self._core_session_factory.create(
            session_id=session_id,
            character_id=str(request["character_id"]),
            conversation_id=UUID(str(request["conversation_id"])),
            delivery=delivery,
        )
        bridge = _ConversationCoreBridge(
            core_session,
            lambda operation: self._schedule_task(session_id, operation),
        )
        self._audio_sources[session_id] = audio_source
        self._core_sessions[session_id] = core_session
        self._core_bridges[session_id] = bridge
        if isinstance(self._core_port, ProductionCoreEventInbox):
            self._core_port.bind(session_id, bridge.notify)
        self._ready[session_id].set()

    async def _observe_microphone(
        self,
        session_id: str,
        track: rtc.Track,
        coordinator: ProductionSessionCoordinator,
        participant_identity: str,
        participant_sid: str,
        generation: int,
        publish_data: Callable[[bytes, str], Awaitable[None]],
    ) -> None:
        rtc_module = _livekit_rtc_module()

        def schedule_observation(operation: Awaitable[None]) -> None:
            self._schedule_task(session_id, operation)

        observer = MicrophoneTrackObserver(
            observation_port=_ObservationPublisher(
                publish_data,
                lambda: coordinator.generation,
                schedule_observation,
            ),
            sample_rate=STT_SAMPLE_RATE,
        )
        stream: rtc.AudioStream = rtc_module.AudioStream(
            track, sample_rate=STT_SAMPLE_RATE, num_channels=1
        )
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
                bridge = self._core_bridges.get(session_id)
                if bridge is None:
                    await self._ready[session_id].wait()
                    bridge = self._core_bridges.get(session_id)
                    if bridge is None:
                        return
                pcm = bytes(frame.data)
                bridge.receive_microphone(pcm)
                observer.receive_frame(
                    pcm=pcm,
                    sample_count=int(frame.samples_per_channel),
                    received_at_ms=int(time.monotonic() * 1000),
                )
                if session_id not in self._rooms:
                    return
        finally:
            await stream.aclose()

    def _schedule_task(
        self, session_id: str, coroutine: Awaitable[None]
    ) -> asyncio.Task[None] | None:
        tasks = self._session_tasks.get(session_id)
        if tasks is None:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            elif isinstance(coroutine, asyncio.Future):
                coroutine.cancel()
            return None
        task = asyncio.create_task(self._run_owned_task(session_id, coroutine))
        tasks.add(task)
        task.add_done_callback(lambda completed: self._task_done(tasks, completed))
        return task

    def _schedule_serialized_participant_operation(
        self,
        session_id: str,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        previous = self._participant_event_tails.get(session_id)

        async def run_serialized() -> None:
            if previous is not None:
                await previous
            await operation()

        task = self._schedule_task(session_id, run_serialized())
        if task is None:
            return
        self._participant_event_tails[session_id] = task
        task.add_done_callback(
            lambda completed: self._participant_operation_done(
                session_id, completed
            )
        )

    def _participant_operation_done(
        self, session_id: str, completed: asyncio.Task[None]
    ) -> None:
        if self._participant_event_tails.get(session_id) is completed:
            self._participant_event_tails.pop(session_id, None)

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

    async def _prepare_output_track(
        self, room: rtc.Room
    ) -> _LiveKitPcmAudioSource:
        rtc_module = _livekit_rtc_module()

        source: rtc.AudioSource = rtc_module.AudioSource(PCM_SAMPLE_RATE, PCM_CHANNELS)
        track = rtc_module.LocalAudioTrack.create_audio_track("character-response", source)
        options = rtc_module.TrackPublishOptions(
            source=rtc_module.TrackSource.SOURCE_MICROPHONE
        )
        await room.local_participant.publish_track(track, options)
        return _LiveKitPcmAudioSource(source)

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
    ) -> tuple[rtc.Room | None, list[asyncio.Task[None]]]:
        self._coordinators.pop(session_id, None)
        self._participant_event_tails.pop(session_id, None)
        tasks = self._session_tasks.pop(session_id, set())
        pending_tasks: list[asyncio.Task[None]] = []
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
                pending_tasks.append(task)
        room = self._rooms.pop(session_id, None)
        self._ready.pop(session_id, None)
        self._audio_sources.pop(session_id, None)
        self._core_bridges.pop(session_id, None)
        if isinstance(self._core_port, ProductionCoreEventInbox):
            self._core_port.unbind(session_id)
        return room, pending_tasks

    async def _cleanup_owned_session(self, session_id: str) -> None:
        state = self._cleanup_states.get(session_id)
        if state is None:
            core_session = self._core_sessions.pop(session_id, None)
            if core_session is not None:
                await core_session.end()
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

            local_results = await asyncio.gather(
                *local_operations, return_exceptions=True
            )
            room_cleanup_result: object = None
            if room_cleanup_task is not None:
                try:
                    await asyncio.shield(room_cleanup_task)
                except Exception as error:
                    room_cleanup_result = error
            for result in local_results:
                if isinstance(result, BaseException):
                    raise result
            if isinstance(room_cleanup_result, BaseException):
                raise RoomCleanupPendingError(
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


async def configure_production_resources(
    app: FastAPI,
    *,
    core_session_factory: _CoreSessionFactory | None,
) -> livekit_api.LiveKitAPI | None:
    settings = resolve_livekit_settings()
    if settings is None:
        return None
    if core_session_factory is None:
        raise RuntimeError("Conversation Core session factory is required")
    livekit_url, api_key, api_secret = settings
    api = _livekit_api_module()

    client: livekit_api.LiveKitAPI = api.LiveKitAPI(livekit_url, api_key, api_secret)
    room_manager = ProductionRoomManager(client)
    signer = ProductionTokenSigner(api_key, api_secret)
    sessions = InMemorySessionBindingRepository()
    core_events = ProductionCoreEventInbox()
    runtime = ProductionRuntimeManager(
        livekit_url=livekit_url,
        signer=signer,
        room_manager=room_manager,
        session_repository=sessions,
        core_port=core_events,
        core_session_factory=core_session_factory,
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
    return client

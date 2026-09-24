"""session単位の資源所有点と、session IDから所有点への到達を担うruntime manager。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from app.conversation_core import ConversationCoreSession
from app.livekit_transport.bootstrap import (
    BOOTSTRAP_TIMEOUT_SECONDS,
    InMemorySessionBindingRepository,
    preparation_operation,
)
from app.livekit_transport.coordinator import (
    APPLICATION_TOPIC,
    PRIVATE_TOPIC,
    SCREEN_TOPIC,
    ProductionSessionCoordinator,
    SessionCoordinatorDependencies,
)
from app.livekit_transport.core_delivery import (
    ProductionCoreEventInbox,
    _ConversationCoreDelivery,
)
from app.livekit_transport.core_factory import (
    _CoreSessionFactory,
    _MissingCoreSessionFactory,
)
from app.livekit_transport.delivery import CoreNotificationPort
from app.livekit_transport.errors import RoomCleanupPendingError
from app.livekit_transport.microphone_bridge import _ConversationCoreBridge
from app.livekit_transport.microphone_reader import MicrophoneReaderOwner
from app.livekit_transport.production_sdk import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    ProductionRoomManager,
    ProductionTokenSigner,
    _required_int,
    livekit_rtc_module,
)
from app.livekit_transport.response_audio import ResponseAudioTracks
from app.livekit_transport.text_input import TextInputReceiver
from app.voice_metrics import JsonlTraceRecorder, MeasurementKind
from app.voice_session_metrics import SessionMetrics

if TYPE_CHECKING:
    import livekit.rtc as rtc

logger = logging.getLogger(__name__)


class _ScreenSessionRevoker(Protocol):
    async def revoke_client(self, client_session_id: UUID, reason: str) -> None: ...


@dataclass
class _SessionCleanupState:
    room: rtc.Room | None
    pending_runtime_tasks: list[asyncio.Task[None]]
    audio_source: ResponseAudioTracks | None = None
    session_binding_deleted: bool = False
    room_deleted: bool = False
    room_cleanup_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ProductionSessionOwner:
    """接続開始からcleanup完了まで同じsessionの資源を保持する所有点。

    認可・generation・participantの正本は`coordinator`、入力grantは`bridge`側に残る。
    ownerはそれらを複製せず、Room・task・reader・cleanup checkpointの所有点としてだけ使う。
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.room: rtc.Room | None = None
        self.coordinator: ProductionSessionCoordinator | None = None
        self.ready: asyncio.Event | None = None
        self.tasks: set[asyncio.Task[None]] | None = set()
        self.participant_tail: asyncio.Task[None] | None = None
        self.audio_source: ResponseAudioTracks | None = None
        self.core_session: ConversationCoreSession | None = None
        self.bridge: _ConversationCoreBridge | None = None
        self.screen_client_session_id: UUID | None = None
        self.publish_data: Callable[[bytes, str], Awaitable[None]] | None = None
        self.readers = MicrophoneReaderOwner(self)
        self.cleanup: _SessionCleanupState | None = None

    def schedule_task(
        self, coroutine: Awaitable[None]
    ) -> asyncio.Task[None] | None:
        tasks = self.tasks
        if tasks is None:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            elif isinstance(coroutine, asyncio.Future):
                coroutine.cancel()
            return None
        task = asyncio.create_task(self._run_owned_task(coroutine))
        tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def schedule_serialized_participant_operation(
        self,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        previous = self.participant_tail

        async def run_serialized() -> None:
            if previous is not None:
                await previous
            await operation()

        task = self.schedule_task(run_serialized())
        if task is None:
            return
        self.participant_tail = task
        task.add_done_callback(self._participant_operation_done)

    def _participant_operation_done(
        self, completed: asyncio.Task[None]
    ) -> None:
        if self.participant_tail is completed:
            self.participant_tail = None

    async def _run_owned_task(self, operation: Awaitable[None]) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception:
            coordinator = self.coordinator
            if coordinator is not None and coordinator.phase != "ended":
                await coordinator.cleanup("runtime_error")
            raise

    def _task_done(self, completed: asyncio.Task[None]) -> None:
        tasks = self.tasks
        if tasks is not None:
            tasks.discard(completed)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.error(
                "LiveKit session task failed",
                exc_info=(type(error), error, error.__traceback__),
            )


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
        screen_session_revoker: _ScreenSessionRevoker | None = None,
        audio_probe_enabled: bool = False,
        session_trace_recorder: JsonlTraceRecorder | None = None,
        measurement_kind: MeasurementKind = "automated_test",
    ) -> None:
        self._session_trace_recorder = session_trace_recorder
        self._measurement_kind = measurement_kind
        self._audio_probe_enabled = audio_probe_enabled
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
        self._screen_session_revoker = screen_session_revoker
        self._owners: dict[str, ProductionSessionOwner] = {}

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
                **(
                    {}
                    if reservation.request.get("screen_client_session_id") is None
                    else {
                        "screen_client_session_id": reservation.request[
                            "screen_client_session_id"
                        ]
                    }
                ),
            }
        )

    async def wait_until_ready(self, session_id: str) -> None:
        owner = self._owners[session_id]
        ready = owner.ready
        if ready is None:
            raise RuntimeError("session readiness is not tracked")
        async with preparation_operation("readiness", BOOTSTRAP_TIMEOUT_SECONDS):
            await ready.wait()

    async def start_runtime(self, request: dict[str, object]) -> None:
        rtc_module = livekit_rtc_module()

        session_id = str(request["session_id"])
        owner = self._owners.get(session_id)
        if owner is None:
            owner = ProductionSessionOwner(session_id)
            self._owners[session_id] = owner
        if request.get("screen_client_session_id") is not None:
            owner.screen_client_session_id = UUID(
                str(request["screen_client_session_id"])
            )
        room_name = f"voice-{session_id}"
        user_identity = f"user-{session_id}"
        async with preparation_operation("token", BOOTSTRAP_TIMEOUT_SECONDS):
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

        if self._audio_probe_enabled:
            room_observation_count = 0

            def observe_room_stage(stage: str) -> None:
                nonlocal room_observation_count
                if room_observation_count > 128:
                    return
                if room_observation_count == 128:
                    stage = "overflow"
                room_observation_count += 1
                # SDKの接続先・identity・例外本文は診断へ渡さない。
                logger.warning("RTC lifecycle: stage=%s at_ms=%d", stage, time.monotonic_ns() // 1_000_000)

            room.on("connected")(lambda: observe_room_stage("connected"))
            room.on("reconnecting")(lambda: observe_room_stage("reconnecting"))
            room.on("reconnected")(lambda: observe_room_stage("reconnected"))
            room.on("disconnected")(lambda _reason: observe_room_stage("disconnected"))

        async def publish_data(payload: bytes, topic: str) -> None:
            await room.local_participant.publish_data(payload, reliable=True, topic=topic)
        owner.publish_data = publish_data

        async def cleanup(_owned_session_id: str) -> None:
            await self._cleanup_owned_session(session_id)

        async def generation_ready() -> None:
            if audio_probe is not None:
                await audio_probe.cancel()
            await owner.readers.synchronize()

        def response_track_ready(response_id: str, track_sid: str) -> None:
            if owner.audio_source is not None:
                owner.audio_source.confirm_ready(response_id, track_sid)

        from app.livekit_transport.audio_probe import AudioProbePublisher
        audio_probe = AudioProbePublisher(room,
            current=lambda generation: coordinator.phase == "available" and coordinator.generation == generation,
            publish=lambda frame: publish_data(json.dumps(frame).encode(), PRIVATE_TOPIC),
            schedule=lambda operation: owner.schedule_task(operation),
        ) if self._audio_probe_enabled else None

        def handle_audio_probe(kind: str, probe_id: str, generation: int, sid: str | None) -> None:
            if audio_probe is None:
                return
            if kind == "audio_probe_request":
                audio_probe.request(probe_id, generation)
            elif sid is not None:
                audio_probe.receive(kind, probe_id, generation, sid)

        sync_observation_count = 0

        def observe_sync(stage: str, generation: int, at_ms: int) -> None:
            nonlocal sync_observation_count
            if sync_observation_count > 512:
                return
            if sync_observation_count == 512:
                stage = "overflow"
            sync_observation_count += 1
            # 専用test Profileのみ。本文、ID、接続先、例外文字列を含めない。
            logger.warning("State sync: stage=%s generation=%d at_ms=%d", stage, generation, at_ms)

        session_metrics = (
            SessionMetrics(character_id=str(request["character_id"]), session_id=session_id,
                           measurement_kind=self._measurement_kind,
                           record=self._session_trace_recorder.record_session)
            if self._session_trace_recorder is not None else None
        )
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
                response_track_ready=response_track_ready,
                audio_probe=handle_audio_probe if audio_probe is not None else None,
                sync_observer=observe_sync if self._audio_probe_enabled else None,
                session_metrics=session_metrics,
            ),
            core_port=self._core_port,
        )
        owner.room = room
        owner.coordinator = coordinator
        owner.tasks = set()
        owner.ready = asyncio.Event()
        rtc_diagnostic = None
        if self._audio_probe_enabled:
            from app.livekit_transport.rtc_diagnostic import RtcIngressDiagnostic
            rtc_diagnostic = RtcIngressDiagnostic(lambda: coordinator.generation)
            stats_task: asyncio.Task[None] | None = None

            def start_rtc_diagnostic() -> None:
                nonlocal stats_task
                if stats_task is None:
                    stats_task = owner.schedule_task(rtc_diagnostic.sample(room))

            room.on("reconnecting")(start_rtc_diagnostic)

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

            owner.schedule_serialized_participant_operation(
                handle_connected
            )
        room.on("participant_connected")(participant_connected)

        def participant_disconnected(participant: rtc.RemoteParticipant) -> None:
            async def handle_disconnected() -> None:
                coordinator.participant_disconnected(
                    identity=str(participant.identity),
                    participant_sid=str(participant.sid),
                )

            owner.schedule_serialized_participant_operation(
                handle_disconnected
            )

        room.on("participant_disconnected")(participant_disconnected)

        def data_received(packet: rtc.DataPacket) -> None:
            participant = getattr(packet, "participant", None)
            if rtc_diagnostic is not None:
                participant_kind = "missing" if participant is None else "current" if coordinator.is_current_participant(
                    identity=str(participant.identity), participant_sid=str(participant.sid)) else "other"
                rtc_diagnostic.ingress({PRIVATE_TOPIC: "private", APPLICATION_TOPIC: "application", SCREEN_TOPIC: "screen"}.get(
                    str(packet.topic), "other"), participant_kind)
            if participant is None:
                return
            owner.schedule_task(
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
                await owner.readers.subscribe(
                    track, str(participant.identity), str(participant.sid)
                )

            owner.schedule_serialized_participant_operation(
                handle_track_subscribed
            )

        room.on("track_subscribed")(track_subscribed)

        def track_unsubscribed(
            track: rtc.Track,
            _publication: rtc.RemoteTrackPublication,
            _participant: rtc.RemoteParticipant,
        ) -> None:
            async def handle_track_unsubscribed() -> None:
                await owner.readers.unsubscribe(track)

            owner.schedule_serialized_participant_operation(
                handle_track_unsubscribed
            )

        room.on("track_unsubscribed")(track_unsubscribed)

        def startup_ended() -> bool:
            return (
                owner.coordinator is not coordinator
                or coordinator.phase == "ended"
            )

        async with preparation_operation("transport", BOOTSTRAP_TIMEOUT_SECONDS):
            await room.connect(self._livekit_url, token)
        if startup_ended():
            raise RuntimeError("runtime startup ended")
        async with preparation_operation("output", BOOTSTRAP_TIMEOUT_SECONDS):
            audio_source = await self._prepare_output_track(room)
        if startup_ended():
            await audio_source.aclose()
            raise RuntimeError("runtime startup ended")
        owner.audio_source = audio_source
        delivery = _ConversationCoreDelivery(
            coordinator=coordinator,
            audio_source=audio_source,
            character_participant_id=str(uuid4()),
            character_id=str(request["character_id"]),
            user_participant_id=str(request["core_participant_id"]),
        )
        create_session = getattr(
            self._core_session_factory, "create_ready", self._core_session_factory.create,
        )
        created_session = create_session(
            session_id=session_id,
            character_id=str(request["character_id"]),
            conversation_id=UUID(str(request["conversation_id"])),
            delivery=delivery,
            client_session_id=(
                UUID(str(request["screen_client_session_id"]))
                if request.get("screen_client_session_id") is not None
                else None
            ),
        )

        core_session = (
            await created_session if inspect.isawaitable(created_session) else created_session
        )
        # await中の期限切れ・終了後は、遅れて完成したCoreを登録せず閉じる。
        if startup_ended():
            await core_session.end()
            raise RuntimeError("runtime startup ended")

        def schedule_core_operation(operation: Awaitable[None]) -> None:
            owner.schedule_task(operation)

        async def submit_text(input_id: str, text: str) -> str | None:
            discarded = bridge.invalidate_unfinalized_audio()
            response = await core_session.submit_text(
                input_id=input_id, text=text, discard_speech_ids=discarded,
            )
            return response.response_id if response is not None else None

        async def publish_input_result(event: dict[str, object]) -> None:
            await coordinator.send_core(json.dumps(event, ensure_ascii=False).encode())

        bridge = _ConversationCoreBridge(
            core_session,
            schedule_core_operation,
            stop_audio=audio_source.clear,
            confirm_response_playback=delivery.confirm_response_playback,
            measurement=delivery.measurement,
            session_metrics=session_metrics,
            publish_audio_event=publish_input_result,
            authorize_microphone=owner.readers.authorize,
            verify_audio_integrity=owner.readers.verify,
            user_participant_id=str(request["core_participant_id"]),
            text_input=TextInputReceiver(
                session_id=session_id, participant_id=str(request["core_participant_id"]),
                submit=submit_text, publish=publish_input_result,
                accepting_input=lambda: core_session.accepting_input,
            ),
        )
        owner.core_session = core_session
        owner.bridge = bridge
        await bridge.prepare_audio()
        if startup_ended():
            # cleanup後に完成した入力も閉じ、終了Sessionへhandlerを再登録しない。
            await bridge.close_audio()
            raise RuntimeError("runtime startup ended")
        if isinstance(self._core_port, ProductionCoreEventInbox):
            self._core_port.bind(session_id, bridge.notify)
        # clientのjoin猶予をモデル準備で消費しない。
        coordinator.start_join_deadline()
        owner.ready.set()

    async def _prepare_output_track(self, room: rtc.Room) -> ResponseAudioTracks:
        # 応答決定まで無所属の出力trackを発行しない。
        return ResponseAudioTracks(room, sample_rate=PCM_SAMPLE_RATE, channels=PCM_CHANNELS)

    async def send_core(self, session_id: str, payload: bytes) -> None:
        owner = self._owners.get(session_id)
        coordinator = owner.coordinator if owner is not None else None
        if coordinator is None:
            raise RuntimeError("LiveKit session is not active")
        await coordinator.send_core(payload)

    async def send_screen(self, session_id: str, payload: bytes) -> None:
        owner = self._owners.get(session_id)
        coordinator = owner.coordinator if owner is not None else None
        if coordinator is None:
            raise RuntimeError("LiveKit session is not active")
        await coordinator.send_screen(payload)

    def confirmation_session(self, character: str, conversation: str) -> str | None:
        """管理画面の選択中スレッドによらず、要求を所有する音声sessionへ続行する。"""
        for session_id, owner in self._owners.items():
            if owner.core_session is None:
                continue
            reservation = self._sessions.get(session_id)
            if (
                reservation is not None
                and str(reservation.request["character_id"]) == character
                and str(reservation.request["conversation_id"]) == conversation
            ):
                return session_id
        return None

    async def submit_action_confirmation(
        self, session_id: str, character: str, conversation: str,
        request_id: str, message: str, still_waiting: Callable[[], bool],
    ) -> bool:
        """画面操作から音声応答を開始する。STT入力や終了済み活動を再開しない。"""
        reservation = self._sessions.get(session_id)
        owner = self._owners.get(session_id)
        core = owner.core_session if owner is not None else None
        if (
            reservation is None or core is None
            or str(reservation.request["character_id"]) != character
            or str(reservation.request["conversation_id"]) != conversation
        ):
            raise ValueError("voice_confirmation_session_mismatch")
        # 確認の読み上げが終わるのを待ち、既存応答と入力を混ぜない。
        async with asyncio.timeout(30):
            while core.active_response is not None:
                if not core.accepting_input or not still_waiting():
                    return False
                await asyncio.sleep(0.05)
        if not core.accepting_input or not still_waiting():
            return False
        response = await core.finalize_utterance(
            utterance_id=request_id, transcript=message, should_response=True,
            control_request_id=request_id,
            control_input_valid=still_waiting,
        )
        return response is not None

    async def stop(self, session_id: str) -> None:
        owner = self._owners.get(session_id)
        coordinator = owner.coordinator if owner is not None else None
        if coordinator is not None:
            await coordinator.cleanup("explicit")
        else:
            await self._cleanup_owned_session(session_id)

    async def stop_all(self) -> None:
        for session_id in set(self._owners):
            await self.stop(session_id)

    def _release_owner_resources(
        self, owner: ProductionSessionOwner
    ) -> tuple[rtc.Room | None, list[asyncio.Task[None]]]:
        owner.coordinator = None
        owner.participant_tail = None
        tasks = owner.tasks if owner.tasks is not None else set()
        owner.tasks = None
        pending_tasks: list[asyncio.Task[None]] = []
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
                pending_tasks.append(task)
        room = owner.room
        owner.room = None
        owner.ready = None
        owner.audio_source = None
        owner.bridge = None
        core_port = getattr(self, "_core_port", None)
        if isinstance(core_port, ProductionCoreEventInbox):
            core_port.unbind(owner.session_id)
        return room, pending_tasks

    async def _cleanup_owned_session(self, session_id: str) -> None:
        owner = self._owners.get(session_id)
        if owner is None:
            owner = ProductionSessionOwner(session_id)
            self._owners[session_id] = owner
        if owner.cleanup is None:
            screen_client_session_id = owner.screen_client_session_id
            owner.screen_client_session_id = None
            if screen_client_session_id is not None and self._screen_session_revoker is not None:
                await self._screen_session_revoker.revoke_client(
                    screen_client_session_id,
                    "backend_disconnect",
                )
            bridge = owner.bridge
            if bridge is not None:
                await bridge.close_audio()
            core_session = owner.core_session
            owner.core_session = None
            if core_session is not None:
                await core_session.end()
            audio_source = owner.audio_source
            room, pending_tasks = self._release_owner_resources(owner)
            owner.cleanup = _SessionCleanupState(
                room=room,
                pending_runtime_tasks=pending_tasks,
                audio_source=audio_source,
            )
        state = owner.cleanup

        async with state.lock:
            room_cleanup_task = self._start_room_cleanup(session_id, state)
            local_operations: list[Awaitable[object]] = []
            if state.audio_source is not None:
                local_operations.append(asyncio.create_task(self._close_audio_source(state)))
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
            if self._owners.get(session_id) is owner and owner.cleanup is state:
                self._owners.pop(session_id)

    @staticmethod
    async def _close_audio_source(state: _SessionCleanupState) -> None:
        if state.audio_source is not None:
            await state.audio_source.aclose()
            state.audio_source = None

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

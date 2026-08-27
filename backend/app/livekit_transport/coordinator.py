from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from app.livekit_transport.delivery import (
    CoreEventDelivery,
    CoreNotificationPort,
    EventDeduplicator,
    EventSequenceTracker,
    RetryState,
    TerminalProtocolError,
    decode_core_event,
    decode_private_frame,
    reconnect_sync_frames,
    retry_deadlines_ms,
)
from app.livekit_transport.lifecycle import SessionLifecycle
from app.livekit_transport.mapping import ParticipantMapping
from app.livekit_transport.outbox import (
    InMemoryOutboxManager,
    OutboxCapacityExceeded,
)


APPLICATION_TOPIC = "digital-souls.core.v1"
PRIVATE_TOPIC = "digital-souls.livekit-transport.v1"
OUTBOX_MAX_EVENTS = 256
OUTBOX_MAX_BYTES = 1024 * 1024


def _required_int(value: object, field: str) -> int:
    if not isinstance(value, int):
        raise TerminalProtocolError(f"{field} must be an integer")
    return value


@dataclass(frozen=True)
class SessionCoordinatorDependencies:
    publish_data: Callable[[bytes, str], Awaitable[None]]
    cleanup: Callable[[str], Awaitable[None]]
    generation_ready: Callable[[], Awaitable[None]]


class ProductionSessionCoordinator:
    def __init__(
        self,
        *,
        session_id: str,
        user_identity: str,
        core_participant_id: str,
        reconnect_grace_ms: int,
        dependencies: SessionCoordinatorDependencies,
        core_port: CoreNotificationPort,
        monotonic_ms: Callable[[], int] = lambda: int(time.monotonic() * 1000),
    ) -> None:
        self.session_id = session_id
        self.user_identity = user_identity
        self._dependencies = dependencies
        self._core_port = core_port
        self._clock = monotonic_ms
        self._mapping = ParticipantMapping()
        self._mapping.bind(
            core_participant_id=core_participant_id,
            identity=user_identity,
            participant_sid="",
            room_sid="",
        )
        self._terminal_outcomes: list[dict[str, object]] = []
        self._lifecycle = SessionLifecycle(
            session_id=session_id,
            reconnect_grace_ms=reconnect_grace_ms,
            notify=self._record_terminal_outcome,
        )
        self._delivery = CoreEventDelivery(core_port=core_port)
        self._outbound_deduplicator = EventDeduplicator(
            max_events=OUTBOX_MAX_EVENTS * 2,
            max_bytes=OUTBOX_MAX_BYTES * 2,
        )
        self._outbound_sequences = EventSequenceTracker()
        self._outboxes = InMemoryOutboxManager(
            max_events=OUTBOX_MAX_EVENTS,
            max_bytes=OUTBOX_MAX_BYTES,
        )
        self._retry_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._deadline_task: asyncio.Task[None] | None = None
        self._ended = False

    @property
    def generation(self) -> int:
        return self._lifecycle.generation

    @property
    def phase(self) -> str:
        return self._lifecycle.phase

    @property
    def pending_retry_count(self) -> int:
        return len(self._retry_tasks)

    def start_join_deadline(self) -> None:
        self._replace_deadline(90)

    def begin_response(self, *, response_id: str) -> None:
        self._lifecycle.begin_response(response_id=response_id)

    def participant_connected(
        self, *, identity: str, participant_sid: str, room_sid: str
    ) -> bool:
        if identity != self.user_identity or self._ended:
            return False
        previous_sid = self._mapping.participant_sid(identity)
        if previous_sid:
            if previous_sid != participant_sid:
                self._mapping.replace_connection(identity, participant_sid)
            else:
                return False
        else:
            self._mapping.bind(
                core_participant_id=self._mapping.core_notification(
                    identity=identity, event_type="participant_connected"
                )["participant_id"],
                identity=identity,
                participant_sid=participant_sid,
                room_sid=room_sid,
            )
        if self._lifecycle.phase == "unavailable":
            self._lifecycle.reconnect(now_ms=self._clock())
            self._cancel_deadline()
            self._notify_core("session_reconnected")
            return True
        elif self._lifecycle.phase == "bootstrapping":
            self._cancel_deadline()
            self._lifecycle.activate()
        return False

    async def synchronize_reconnection(self) -> None:
        await self._send_authoritative_state()

    def is_current_participant(self, *, identity: str, participant_sid: str) -> bool:
        return (
            not self._ended
            and identity == self.user_identity
            and self._mapping.participant_sid(identity) == participant_sid
        )

    def participant_disconnected(self, *, identity: str, participant_sid: str) -> None:
        if (
            identity != self.user_identity
            or self._ended
            or self._mapping.participant_sid(identity) != participant_sid
        ):
            return
        self._lifecycle.disconnect(now_ms=self._clock())
        self._notify_core("session_disconnected")
        self._replace_deadline(self._lifecycle.reconnect_grace_ms / 1000)

    async def receive_data(
        self,
        *,
        identity: str,
        participant_sid: str,
        topic: str,
        payload: bytes,
    ) -> None:
        if not self.is_current_participant(
            identity=identity, participant_sid=participant_sid
        ):
            return
        try:
            if topic == APPLICATION_TOPIC:
                event = decode_core_event(payload)
                if str(event["session_id"]) != self.session_id:
                    raise TerminalProtocolError("Core event session mismatch")
                self._delivery.receive(payload, event)
                if event["type"] in ("playback_completed", "playback_stopped"):
                    self._lifecycle.confirm_playback(
                        response_id=str(event["response_id"]),
                        confirmed_audio_sequence=_required_int(
                            event["last_played_audio_sequence"],
                            "last_played_audio_sequence",
                        ),
                    )
                await self._publish_private(
                    {
                        "protocol_version": "1.0",
                        "type": "ack",
                        "event_id": event["event_id"],
                        "generation": self.generation,
                    }
                )
                return
            if topic == PRIVATE_TOPIC:
                frame = decode_private_frame(payload)
                frame_generation = _required_int(frame["generation"], "generation")
                if frame["type"] == "state_sync_request":
                    if frame_generation > self.generation:
                        return
                    if self._lifecycle.phase == "unavailable":
                        self._lifecycle.reconnect(now_ms=self._clock())
                        self._cancel_deadline()
                        self._notify_core("session_reconnected")
                    elif frame_generation == self.generation:
                        self._lifecycle.advance_generation()
                    await self._send_authoritative_state()
                    await self._dependencies.generation_ready()
                    return
                if frame_generation != self.generation:
                    return
                if frame["type"] == "ack":
                    self.acknowledge(str(frame["event_id"]), "character_to_user")
        except TerminalProtocolError:
            await self.cleanup("protocol_error")
            raise

    async def send_core(self, payload: bytes) -> None:
        if self._lifecycle.phase != "available":
            raise RuntimeError("session is not available")
        try:
            event = decode_core_event(payload)
            if str(event["session_id"]) != self.session_id:
                raise TerminalProtocolError("Core event session mismatch")
            event_id = str(event["event_id"])
            result = self._outbound_deduplicator.classify(event_id, payload)
            if result.status == "duplicate":
                return
            self._outbound_sequences.accept(event)
        except TerminalProtocolError:
            await self.cleanup("protocol_error")
            raise
        outbox = self._outboxes.get(self.session_id, "character_to_user")
        try:
            outbox.enqueue(event_id, payload)
        except OutboxCapacityExceeded:
            await self.mark_unavailable()
            raise
        task = asyncio.create_task(self._retry(event_id, payload))
        self._retry_tasks[("character_to_user", event_id)] = task
        await self._dependencies.publish_data(payload, APPLICATION_TOPIC)

    def acknowledge(self, event_id: str, direction: str) -> bool:
        acknowledged = self._outboxes.get(self.session_id, direction).ack(event_id)
        if acknowledged:
            task = self._retry_tasks.pop((direction, event_id), None)
            if task is not None:
                task.cancel()
        return acknowledged

    async def mark_unavailable(self) -> None:
        if self._ended:
            return
        if self._lifecycle.phase == "available":
            self._lifecycle.disconnect(now_ms=self._clock())
            self._notify_core("session_disconnected")
            self._replace_deadline(self._lifecycle.reconnect_grace_ms / 1000)
        self._cancel_retry_tasks()

    async def cleanup(self, reason: str) -> None:
        if self._ended:
            return
        self._ended = True
        self._cancel_deadline()
        self._cancel_retry_tasks()
        self._outboxes.clear_session(self.session_id)
        self._terminal_outcomes.clear()
        self._mapping.clear()
        self._lifecycle.end(reason)
        await self._dependencies.cleanup(self.session_id)

    async def _retry(self, event_id: str, payload: bytes) -> None:
        retry = RetryState(
            event_id=event_id,
            payload=payload,
            initially_sent_at_ms=0,
        )
        previous_deadline_ms = 0
        try:
            for deadline_ms in retry_deadlines_ms(0):
                await asyncio.sleep((deadline_ms - previous_deadline_ms) / 1000)
                previous_deadline_ms = deadline_ms
                if not self._outboxes.get(
                    self.session_id, "character_to_user"
                ).contains(event_id):
                    return
                attempt = retry.poll(deadline_ms)
                if attempt is not None:
                    try:
                        await self._dependencies.publish_data(
                            attempt.payload, APPLICATION_TOPIC
                        )
                    except Exception:
                        # 一時的なpublish失敗でも残りのdeadlineまで再送を継続する。
                        continue
            if self._outboxes.get(
                self.session_id, "character_to_user"
            ).contains(event_id):
                retry.poll(previous_deadline_ms + 1)
                if not retry.transport_available:
                    await self.mark_unavailable()
        finally:
            self._retry_tasks.pop(("character_to_user", event_id), None)

    async def _send_authoritative_state(self) -> None:
        frames = reconnect_sync_frames(
            authoritative_state={
                "protocol_version": "1.0",
                "generation": self.generation,
                "session_phase": self.phase,
            },
            terminal_outcomes=self._terminal_outcomes,
        )
        for frame in frames:
            await self._publish_private(frame)

    def _record_terminal_outcome(self, outcome: dict[str, object]) -> None:
        self._terminal_outcomes.append(dict(outcome))

    async def _publish_private(self, frame: dict[str, object]) -> None:
        payload = json.dumps(frame, separators=(",", ":")).encode()
        decode_private_frame(payload)
        await self._dependencies.publish_data(payload, PRIVATE_TOPIC)

    def _notify_core(self, event_type: str) -> None:
        payload = json.dumps(
            {
                "protocol_version": "1.0",
                "event_id": str(uuid4()),
                "type": event_type,
                "session_id": self.session_id,
                "monotonic_timestamp_ms": self._clock(),
            },
            separators=(",", ":"),
        ).encode()
        decode_core_event(payload)
        self._core_port.notify(payload)

    def _replace_deadline(self, seconds: float) -> None:
        self._cancel_deadline()
        self._deadline_task = asyncio.create_task(self._expire_after(seconds))

    def _cancel_deadline(self) -> None:
        if self._deadline_task is not None and self._deadline_task is not asyncio.current_task():
            self._deadline_task.cancel()
        self._deadline_task = None

    def _cancel_retry_tasks(self) -> None:
        current = asyncio.current_task()
        for task in tuple(self._retry_tasks.values()):
            if task is not current:
                task.cancel()
        self._retry_tasks = {
            key: task
            for key, task in self._retry_tasks.items()
            if task is current
        }

    async def _expire_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self._lifecycle.phase == "bootstrapping":
            expired = self._lifecycle.expire_never_joined(
                now_ms=self._lifecycle.join_deadline_ms
            )
            reason = "join_token_expired"
        else:
            expired = self._lifecycle.expire(now_ms=self._clock())
            reason = "reconnect_timeout"
        if expired:
            await self.cleanup(reason)

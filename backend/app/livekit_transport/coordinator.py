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

# `output_stop_confirmed` の応答相関に使う最終要求の保持上限。
_OUTPUT_STOP_REQUEST_HISTORY = 64
from app.voice_session_metrics import SessionMetrics
from app.livekit_transport.mapping import ParticipantMapping
from app.livekit_transport.outbox import (
    InMemoryOutboxManager,
    OutboxCapacityExceeded,
)


APPLICATION_TOPIC = "digital-souls.core.v1"
SCREEN_TOPIC = "digital-souls.screen-perception.v1"
PRIVATE_TOPIC = "digital-souls.livekit-transport.v2"
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
    # 通知送信はCore確定の外でsession寿命のtaskとして所有する。
    schedule: Callable[[Awaitable[None], str | None], asyncio.Task[None] | None] = (
        lambda operation, _response_id: asyncio.create_task(operation)
    )
    # 通知送信の失敗は本文なしの応答単位metadataで観測する。Core確定・履歴は変更しない。
    notify_failure: Callable[[str, str], None] = lambda _response_id, _name: None
    response_track_ready: Callable[[str, str], None] = lambda _response_id, _track_sid: None
    audio_probe: Callable[[str, str, int, str | None], None] | None = None
    sync_observer: Callable[[str, int, int], None] | None = None
    session_metrics: SessionMetrics | None = None
    # FEのoutput_stop_confirmed観測を差分記録へ渡す。判定・状態は変更しない。
    output_stop_observation: Callable[[str, int], None] = lambda _response_id, _sequence: None


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
        monotonic_us: Callable[[], int] = lambda: time.monotonic_ns() // 1000,
    ) -> None:
        self.session_id = session_id
        self.user_identity = user_identity
        self._dependencies = dependencies
        self._core_port = core_port
        self._clock = monotonic_ms
        self._clock_us = monotonic_us
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
        self._outbound_deduplicator = EventDeduplicator()
        self._outbound_sequences = EventSequenceTracker()
        self._outboxes = InMemoryOutboxManager(
            max_events=OUTBOX_MAX_EVENTS,
            max_bytes=OUTBOX_MAX_BYTES,
        )
        self._retry_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._deadline_task: asyncio.Task[None] | None = None
        # FE向けprivate通知の送信task。Core確定とは別のsession寿命で所有する。
        self._notify_tasks: set[asyncio.Task[None]] = set()
        # response_id -> (request_id, generation)。FEの確認応答を要求へ対応付ける。
        self._output_stop_requests: dict[str, tuple[str, int]] = {}
        self._ended = False
        self._state_sync_lock = asyncio.Lock()
        self._ready_generation: int | None = None

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
            if self._dependencies.session_metrics is not None:
                self._dependencies.session_metrics.activate()
        return False

    async def synchronize_reconnection(self) -> None:
        async with self._state_sync_lock:
            await self._send_ready_authoritative_state()

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
        self._mapping.replace_connection(identity, "")
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
                if event["type"] not in {
                    "session_start_requested", "session_muted", "session_resumed", "session_ended",
                    "session_reconnect_requested", "audio_input_suppression_changed",
                    "audio_input_open_requested", "user_text_submitted", "user_input_result_requested",
                    "response_cancel_requested", "playback_started", "playback_stopped",
                    "playback_completed", "playback_decode_failed", "observation",
                }:
                    raise TerminalProtocolError("event is owned by Backend")
                if event["type"] in {"user_text_submitted", "user_input_result_requested", "audio_input_open_requested"}:
                    speaker = event.get("speaker")
                    expected = self._mapping.core_notification(
                        identity=identity, event_type="text_input"
                    )["participant_id"]
                    if (
                        not isinstance(speaker, dict)
                        or speaker.get("role") != "user"
                        or speaker.get("participant_id") != expected
                    ):
                        raise TerminalProtocolError("text input participant mismatch")
                self._delivery.receive(payload, event)
                if event.get("measurement") == "session_summary" and self._dependencies.session_metrics is not None:
                    self._dependencies.session_metrics.observe_summary(event.get("session_summary"))

                await self._publish_private(
                    {
                        "protocol_version": "2.0",
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
                    self._observe_sync("request_received")
                    # 再送は同じ要求世代のまま直列化し、準備完了前のavailable通知を防ぐ。
                    async with self._state_sync_lock:
                        self._observe_sync("lock_acquired")
                        if frame_generation > self.generation or self._ended:
                            return
                        if self._lifecycle.phase == "unavailable":
                            self._lifecycle.reconnect(now_ms=self._clock())
                            self._cancel_deadline()
                            self._notify_core("session_reconnected")
                        elif frame_generation == self.generation:
                            self._lifecycle.advance_generation()
                        await self._send_ready_authoritative_state()
                    return
                if frame["type"] == "control_probe":
                    received_us = self._clock_us() if frame.get("observe_clock") is True else None
                    self._observe_sync("probe_received")
                if frame_generation != self.generation:
                    if frame["type"] == "control_probe":
                        self._observe_sync("probe_generation_rejected")
                    return
                if frame["type"] in {"audio_probe_request", "audio_probe_ready", "audio_probe_complete"}:
                    if self._lifecycle.phase == "available" and self._dependencies.audio_probe is not None:
                        self._dependencies.audio_probe(str(frame["type"]), str(frame["probe_id"]), frame_generation,
                            str(frame["track_sid"]) if "track_sid" in frame else None)
                    return
                if frame["type"] == "control_probe":
                    # 疎通確認は世代・再生・Core状態を変えず、現在利用可能な接続だけで応答する。
                    if self._lifecycle.phase == "available":
                        self._observe_sync("probe_ack_started")
                        await self._publish_private({
                            "protocol_version": "2.0",
                            "type": "control_probe_ack",
                            "probe_id": frame["probe_id"],
                            "generation": self.generation,
                            **({"server_received_us": received_us, "server_sent_us": self._clock_us()}
                               if received_us is not None else {}),
                        })
                        self._observe_sync("probe_ack_completed")
                    else:
                        self._observe_sync("probe_unavailable")
                    return
                if frame["type"] == "output_stop_confirmed":
                    self._observe_output_stop(frame)
                    return
                if frame["type"] == "response_track_ready":
                    self._dependencies.response_track_ready(str(frame["response_id"]), str(frame["track_sid"]))
                elif frame["type"] == "ack":
                    self.acknowledge(str(frame["event_id"]), "character_to_user")
        except TerminalProtocolError:
            await self.cleanup("protocol_error")
            raise

    async def send_core(self, payload: bytes) -> None:
        if self._lifecycle.phase in {"unavailable", "ended"}:
            # 切断・終了後の最終通知は送信不能。Coreへ取消を伝え、delivery成功や処理失敗にしない。
            raise asyncio.CancelledError("session is no longer available")
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

    def notify_output_stop(self, response_id: str) -> None:
        """FEへ出力停止を通知する。確認応答は待たず、差分観測だけに使う。

        送信はBEの送出停止確定を妨げないよう、session寿命のtaskへ切り離す。
        滞留・失敗はCore確定や履歴を変えない。
        """
        if self._lifecycle.phase != "available":
            return
        request_id, generation = str(uuid4()), self.generation
        self._output_stop_requests[response_id] = (request_id, generation)
        if len(self._output_stop_requests) > _OUTPUT_STOP_REQUEST_HISTORY:
            oldest = next(iter(self._output_stop_requests))
            self._output_stop_requests.pop(oldest, None)
        frame = {
            "protocol_version": "2.0", "type": "output_stop_request",
            "session_id": self.session_id, "response_id": response_id,
            "request_id": request_id, "generation": generation,
        }
        self._schedule_private_notification(
            frame, "output_stop_notify_failed", response_id,
        )

    def confirm_estimated_playback(self, *, response_id: str, sequence: int) -> None:
        """BE推定の再生済みprefixを再接続用の終端状態へ記録する。"""
        self._lifecycle.confirm_playback(
            response_id=response_id, confirmed_audio_sequence=sequence,
        )

    def _observe_output_stop(self, frame: dict[str, object]) -> None:
        request = self._output_stop_requests.get(str(frame["response_id"]))
        if request is None:
            return
        request_id, generation = request
        prefix = frame.get("last_played_audio_sequence")
        if (
            frame["request_id"] != request_id
            or frame["session_id"] != self.session_id
            or frame["generation"] != generation
            or type(prefix) is not int or prefix < 0
            or frame["output_confirmation"] not in {"output_clock_passed", "never_connected"}
            or (frame["output_confirmation"] == "never_connected" and prefix != 0)
        ):
            return
        self._dependencies.output_stop_observation(
            str(frame["response_id"]), prefix,
        )

    async def send_screen(self, payload: bytes) -> None:
        """画面制御metadataを音声Core eventとは別topicで配送する。"""
        if self._lifecycle.phase != "available":
            raise RuntimeError("session is not available")
        await self._dependencies.publish_data(payload, SCREEN_TOPIC)

    async def send_logical_audio_segment(
        self,
        *,
        response_id: str,
        audio_sequence: int,
        pcm_sample_count: int,
    ) -> None:
        """AudioTrack publish前にtransport内部の再生相関情報を送る。"""
        if self._lifecycle.phase != "available":
            raise RuntimeError("session is not available")
        await self._publish_private(
            {
                "protocol_version": "2.0",
                "type": "logical_audio_segment",
                "response_id": response_id,
                "audio_sequence": audio_sequence,
                "generation": self.generation,
                "pcm_sample_count": pcm_sample_count,
            }
        )

    def send_response_audio_finished(
        self, *, response_id: str, input_sample_count: int,
        captured_sample_count: int, padding_sample_count: int,
    ) -> None:
        """native供給完了の総sample数を送る。ブラウザ再生完了とは区別する。

        送信はCore完了判定の外で行う。滞留・失敗は終端を変えない。
        """
        if self._lifecycle.phase != "available":
            raise RuntimeError("session is not available")
        if input_sample_count + padding_sample_count != captured_sample_count:
            raise ValueError("response source sample conservation failed")
        frame = {
            "protocol_version": "2.0", "type": "response_audio_finished",
            "response_id": response_id, "generation": self.generation,
            "input_sample_count": input_sample_count,
            "captured_sample_count": captured_sample_count,
            "padding_sample_count": padding_sample_count,
        }
        self._schedule_private_notification(
            frame, "response_audio_finished_notify_failed", response_id,
        )

    def _schedule_private_notification(
        self, frame: dict[str, object], failure_name: str, response_id: str,
    ) -> None:
        # 送信例外はこの通知の境界内で応答単位metadataへ記録する。
        # Session所有taskの致命的例外へ伝播させず、Core確定・履歴を変えない。
        async def deliver() -> None:
            try:
                await self._publish_private(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._dependencies.notify_failure(response_id, failure_name)

        self._track_notification_task(self._dependencies.schedule(deliver(), response_id))

    def schedule_observation(self, payload: bytes, *, response_id: str) -> None:
        """計測observationのCore配送をsession寿命taskへ切り離す。

        応答の送出位置計測は記録済みで、配送完了をCore完了の条件にしない。
        `send_core`内の検証・相関・再送と、失敗時のcleanup・unavailable遷移は
        そのまま実行され、残った例外だけを応答単位metadataとして観測する。
        """
        if self._lifecycle.phase != "available":
            return

        async def deliver() -> None:
            try:
                await self.send_core(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._dependencies.notify_failure(response_id, "observation_send_failed")

        self._track_notification_task(self._dependencies.schedule(deliver(), response_id))

    def _track_notification_task(self, task: asyncio.Task[None] | None) -> None:
        if task is not None:
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)

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
        # 切断で届かない通知を滞留させない。
        for task in tuple(self._notify_tasks):
            task.cancel()
        if self._notify_tasks:
            await asyncio.gather(*self._notify_tasks, return_exceptions=True)
        self._notify_tasks.clear()

    async def cleanup(self, reason: str) -> None:
        if self._ended:
            return
        self._ended = True
        self._output_stop_requests.clear()
        self._cancel_deadline()
        self._cancel_retry_tasks()
        # session終了と共に通知送信を止め、残留taskを回収する。
        for task in tuple(self._notify_tasks):
            task.cancel()
        if self._notify_tasks:
            await asyncio.gather(*self._notify_tasks, return_exceptions=True)
        self._notify_tasks.clear()
        self._outboxes.clear_session(self.session_id)
        self._terminal_outcomes.clear()
        self._mapping.clear()
        self._lifecycle.end(reason)
        try:
            await self._dependencies.cleanup(self.session_id)
        except BaseException:
            if self._dependencies.session_metrics is not None:
                self._dependencies.session_metrics.end(reason, cleanup_completed=False)
            raise
        else:
            if self._dependencies.session_metrics is not None:
                self._dependencies.session_metrics.end(reason)

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

    async def _send_ready_authoritative_state(self) -> None:
        generation = self.generation
        if self._ended:
            return
        if self._ready_generation != generation:
            self._observe_sync("ready_started")
            await self._dependencies.generation_ready()
            self._observe_sync("ready_completed")
            self._ready_generation = generation
        # 準備中のparticipant再接続・終了は、新世代が準備済みだと補完しない。
        if self._ended or self.generation != generation:
            return
        self._observe_sync("send_started")
        await self._send_authoritative_state()
        self._observe_sync("send_completed")

    def _observe_sync(self, stage: str) -> None:
        if self._dependencies.sync_observer is not None:
            self._dependencies.sync_observer(stage, self.generation, self._clock())

    async def _send_authoritative_state(self) -> None:
        frames = reconnect_sync_frames(
            authoritative_state={
                "protocol_version": "2.0",
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
        if event_type in {"session_disconnected", "session_ended"}:
            # 停止要求の応答相関履歴は接続世代をまたいで使い回さない。
            self._output_stop_requests.clear()
        payload = json.dumps(
            {
                "protocol_version": "2.0",
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

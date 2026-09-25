"""microphone readerの生成・差替え・停止を一つの所有点へ集約する。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.livekit_transport.coordinator import PRIVATE_TOPIC
from app.livekit_transport.microphone_bridge import STT_SAMPLE_RATE
from app.livekit_transport.microphone_frames import (
    MICROPHONE_FRAME_MS,
    MICROPHONE_QUEUE_CAPACITY,
    MicrophoneFrameClock,
)
from app.livekit_transport.microphone_integrity import MicrophoneIntegrity
from app.livekit_transport.production_sdk import livekit_rtc_module
from app.livekit_transport.runtime import MicrophoneTrackObserver
from app.voice_input.pipeline import AudioInputFault

if TYPE_CHECKING:
    import livekit.rtc as rtc

    from app.livekit_transport.session_runtime import ProductionSessionOwner

logger = logging.getLogger(__name__)


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
            "protocol_version": "2.0",
            "type": "microphone_observation",
            "generation": self._generation(),
            **observation,
        }
        self._schedule(
            self._publish(json.dumps(frame, separators=(",", ":")).encode(), PRIVATE_TOPIC)
        )


class MicrophoneReaderOwner:
    """session内のmicrophone readerを所有し、track差替えとintegrity monitorを管理する。

    認可・generationの正本はsession ownerのcoordinatorに残し、この所有点は
    reader taskとtrack統計だけを保持する。
    """

    def __init__(self, owner: ProductionSessionOwner) -> None:
        self._owner = owner
        # id(track) → (track, identity, participant_sid, generation, task)
        self._readers: dict[
            int, tuple[rtc.Track, str, str, int, asyncio.Task[None] | None]
        ] = {}
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        # track_sid → 受信PCMとtrack統計の照合monitor
        self._integrities: dict[str, MicrophoneIntegrity] = {}

    async def subscribe(
        self, track: rtc.Track, identity: str, participant_sid: str
    ) -> None:
        async with self._lock:
            await self._replace(track, identity, participant_sid)

    async def unsubscribe(self, track: rtc.Track) -> None:
        async with self._lock:
            old = self._readers.pop(id(track), None)
            self._changed.set()
            if old is not None and old[4] is not None:
                old[4].cancel("microphone_track_unsubscribed")
                await asyncio.gather(old[4], return_exceptions=True)

    async def synchronize(self) -> None:
        async with self._lock:
            for track, identity, participant_sid, _generation, _task in tuple(
                self._readers.values()
            ):
                await self._replace(track, identity, participant_sid)

    async def _replace(
        self, track: rtc.Track, identity: str, participant_sid: str
    ) -> None:
        coordinator = self._owner.coordinator
        key = id(track)
        old = self._readers.get(key)
        if (
            coordinator is not None
            and old is not None
            and old[1] == identity
            and old[2] == participant_sid
            and old[3] == coordinator.generation
            and old[4] is not None
            and not old[4].done()
            and coordinator.is_current_participant(
                identity=identity, participant_sid=participant_sid
            )
        ):
            return
        self._readers.pop(key, None)
        if old is not None and old[4] is not None:
            old[4].cancel("microphone_track_replaced")
            await asyncio.gather(old[4], return_exceptions=True)
        if coordinator is None or not coordinator.is_current_participant(
            identity=identity, participant_sid=participant_sid
        ):
            return
        generation = coordinator.generation
        task = self._owner.schedule_task(
            self.observe(track, identity, participant_sid, generation)
        )
        if task is not None:
            self._readers[key] = (track, identity, participant_sid, generation, task)
            self._changed.set()

    async def authorize(self, track_sid: str) -> bool:
        coordinator = self._owner.coordinator
        if coordinator is None:
            return False
        try:
            # FEの5秒ACK待ちに収める。統計開始前の発話を認可し、後から欠測で
            # 破棄する状態を避けるため、無音trackで最初の有効統計を待つ。
            async with asyncio.timeout(4):
                while True:
                    for track, identity, participant_sid, generation, task in tuple(
                        self._readers.values()
                    ):
                        if (
                            str(track.sid) == track_sid
                            and task is not None
                            and not task.done()
                            and generation == coordinator.generation
                            and coordinator.is_current_participant(
                                identity=identity, participant_sid=participant_sid
                            )
                        ):
                            monitor = self._integrities.get(track_sid)
                            if monitor is None:
                                # reader taskは登録直後、最初の実行をまだ待っている場合がある。
                                await asyncio.sleep(0.01)
                                break
                            await monitor.wait_ready()
                            return (
                                not task.done()
                                and generation == coordinator.generation
                                and self._integrities.get(track_sid) is monitor
                                and coordinator.is_current_participant(
                                    identity=identity, participant_sid=participant_sid
                                )
                            )
                    else:
                        self._changed.clear()
                        await self._changed.wait()
        except (TimeoutError, AudioInputFault):
            return False

    async def verify(
        self, track_sid: str, start_sample: int, end_sample: int
    ) -> None:
        monitor = self._integrities.get(track_sid)
        if monitor is None:
            raise AudioInputFault("audio_integrity_unavailable")
        await monitor.verify(start_sample, end_sample)

    async def observe(
        self,
        track: rtc.Track,
        participant_identity: str,
        participant_sid: str,
        generation: int,
    ) -> None:
        coordinator = self._owner.coordinator
        if coordinator is None:
            return
        owner = self._owner
        publish_data = owner.publish_data
        if publish_data is None:
            return
        rtc_module = livekit_rtc_module()

        def schedule_observation(operation: Awaitable[None]) -> None:
            owner.schedule_task(operation)

        observer = MicrophoneTrackObserver(
            observation_port=_ObservationPublisher(
                publish_data,
                lambda: coordinator.generation,
                schedule_observation,
            ),
            sample_rate=STT_SAMPLE_RATE,
        )
        clock = MicrophoneFrameClock()
        stream: rtc.AudioStream = rtc_module.AudioStream(
            track, sample_rate=STT_SAMPLE_RATE, num_channels=1,
            capacity=MICROPHONE_QUEUE_CAPACITY, frame_size_ms=MICROPHONE_FRAME_MS,
            noise_cancellation=clock,
        )
        track_sid = str(track.sid)
        monitor = MicrophoneIntegrity(track.get_stats, lambda: clock.samples_seen)
        self._integrities[track_sid] = monitor
        monitor_task = asyncio.create_task(monitor.run())
        exit_reason = "stream_ended"
        try:
            async for event in stream:
                if (
                    coordinator.generation != generation
                    or not coordinator.is_current_participant(
                        identity=participant_identity,
                        participant_sid=participant_sid,
                    )
                ):
                    exit_reason = "participant_replaced"
                    return
                frame = event.frame
                bridge = owner.bridge
                if bridge is None:
                    exit_reason = "bridge_closed"
                    return
                try:
                    pcm, start_sample = clock.read(frame)
                except AudioInputFault as error:
                    exit_reason = error.code
                    # 本文やPCMを出さず、時計検証失敗と通常のreader終了を区別する。
                    logger.warning("LiveKit microphone frame rejected: reason=%s", error.code)
                    return
                await bridge.receive_microphone_frame(
                    pcm, start_sample=start_sample, track_sid=track_sid,
                )
                observer.receive_frame(
                    pcm=pcm,
                    sample_count=int(frame.samples_per_channel),
                    received_at_ms=int(time.monotonic() * 1000),
                )
                if owner.room is None:
                    exit_reason = "room_closed"
                    return
        except asyncio.CancelledError as error:
            permitted = {"microphone_track_replaced", "microphone_track_unsubscribed"}
            exit_reason = error.args[0] if error.args and isinstance(error.args[0], str) and error.args[0] in permitted else "cancelled"
            raise
        except Exception as error:
            exit_reason = type(error).__name__
            # 端末trackの読取失敗はfinallyで入力停止として通知する。
            # text・終了済み発話・回答再生はこのreaderの所有物ではない。
            logger.warning("LiveKit microphone reader failed")
        finally:
            monitor.close()
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
            if self._integrities.get(track_sid) is monitor:
                self._integrities.pop(track_sid, None)
            bridge = owner.bridge
            if bridge is not None:
                bridge.close_microphone_track(track_sid, reader_reason=exit_reason)
            await stream.aclose()

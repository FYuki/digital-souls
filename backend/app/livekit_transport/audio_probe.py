"""明示的な専用test Profileだけで使う、有限長の実音声経路診断。"""
from __future__ import annotations

import asyncio
import importlib
import logging
import math
import struct
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

from app.livekit_transport.paced_audio import PacedPcmSource

logger = logging.getLogger(__name__)
AUDIO_PROBE_TRACK_PREFIX = "ds-audio-probe-v1:"
PROBE_TIMEOUT_SECONDS = 4
READY_TIMEOUT_SECONDS = 1.5
COMPLETE_TIMEOUT_SECONDS = 1
CLEANUP_TIMEOUT_SECONDS = 1


def audio_probe_enabled(environment: Mapping[str, str], livekit_url: str) -> bool:
    return (environment.get("DS_ENVIRONMENT_ID") == "test"
            and environment.get("DS_PROFILE") == "integration-voice-fault"
            and environment.get("VOICE_MEASUREMENT_KIND") == "controlled_baseline"
            and livekit_url in {"ws://127.0.0.1:19880", "ws://localhost:19880"})


def probe_pcm() -> bytes:
    # 利用者入力を使わない200msの固定音。先頭・末尾の5msを減衰させる。
    return b"".join(struct.pack("<h", round(5000 * math.sin(2 * math.pi * 997 * i / 48000)
        * min(1, i / 240, (9599 - i) / 240))) for i in range(9600))


class AudioProbePublisher:
    def __init__(self, room: Any, *, current: Callable[[int], bool],
                 publish: Callable[[dict[str, object]], Awaitable[None]],
                 schedule: Callable[[Awaitable[None]], asyncio.Task[None] | None]) -> None:
        self._room, self._current, self._publish, self._schedule = room, current, publish, schedule
        self._task: asyncio.Task[None] | None = None
        self._probe_id: str | None = None
        self._generation = -1
        self._sid: str | None = None
        self._pending_ready_sid: str | None = None
        self._ready, self._complete = asyncio.Event(), asyncio.Event()
        self._accept_complete = False
        self._seen: set[str] = set()

    def request(self, probe_id: str, generation: int) -> None:
        try:
            valid = str(UUID(probe_id)) == probe_id
        except ValueError:
            valid = False
        if not valid or not self._current(generation):
            return
        if probe_id in self._seen or len(self._seen) >= 8 or (self._task is not None and not self._task.done()):
            return
        self._seen.add(probe_id)
        self._probe_id, self._generation, self._sid, self._pending_ready_sid = probe_id, generation, None, None
        self._ready, self._complete = asyncio.Event(), asyncio.Event()
        self._accept_complete = False
        self._task = self._schedule(self._run(probe_id, generation))

    def receive(self, kind: str, probe_id: str, generation: int, sid: str) -> None:
        if (probe_id != self._probe_id or generation != self._generation or not self._current(generation)
                or self._task is None or self._task.done()):
            return
        if kind == "audio_probe_ready":
            if self._sid is None:
                self._pending_ready_sid = sid
            elif sid == self._sid:
                self._ready.set()
        elif kind == "audio_probe_complete" and sid == self._sid and self._accept_complete:
            self._complete.set()

    async def cancel(self) -> None:
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self, probe_id: str, generation: int) -> None:
        rtc = importlib.import_module("livekit.rtc")
        source = pacer = track = publication = None
        stage = "source"
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                if not self._current(generation):
                    return
                source = rtc.AudioSource(48000, 1, queue_size_ms=0)
                pacer = PacedPcmSource(source)
                track = rtc.LocalAudioTrack.create_audio_track(AUDIO_PROBE_TRACK_PREFIX + probe_id, source)
                stage = "publish_track"
                publication = await self._room.local_participant.publish_track(track,
                    rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE, dtx=False))
                self._sid = publication.sid
                if self._pending_ready_sid == self._sid:
                    self._ready.set()
                stage = "ready"
                await asyncio.wait_for(self._ready.wait(), READY_TIMEOUT_SECONDS)
                if not self._current(generation):
                    return
                stage = "capture"
                await pacer.publish(probe_pcm())
                await pacer.finish()
                if not self._current(generation):
                    return
                stage = "finish_notification"
                self._accept_complete = True
                await self._publish({"protocol_version": "1.0", "type": "audio_probe_finished",
                    "probe_id": probe_id, "generation": generation, "track_sid": publication.sid,
                    "input_sample_count": pacer.input_sample_count, "captured_sample_count": pacer.captured_sample_count,
                    "padding_sample_count": pacer.padding_sample_count})
                stage = "complete"
                await asyncio.wait_for(self._complete.wait(), COMPLETE_TIMEOUT_SECONDS)
        except Exception as error:
            # 診断の送信失敗で会話を終了させない。本文・例外文字列を残さずBrowserの失敗判定を待つ。
            logger.warning("Audio probe failed: stage=%s type=%s", stage, type(error).__name__)
        finally:
            self._accept_complete = False
            try:
                if track is not None:
                    track.mute()
                if pacer is not None:
                    await pacer.aclose()
                if publication is not None and publication.sid in self._room.local_participant.track_publications:
                    try:
                        async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                            await self._room.local_participant.unpublish_track(publication.sid)
                    except rtc.UnpublishTrackError:
                        if publication.sid in self._room.local_participant.track_publications:
                            raise
            finally:
                if source is not None:
                    async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                        await source.aclose()

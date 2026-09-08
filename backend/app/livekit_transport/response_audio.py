"""応答ごとに独立したAudioSourceとAudioTrackを所有する。"""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field
import math
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID

from app.livekit_transport.paced_audio import PacedPcmSource

if TYPE_CHECKING:
    import livekit.rtc as rtc

TRACK_NAME_PREFIX = "ds-response-v1:"


@dataclass
class _ResponseTrack:
    response_id: str
    source: rtc.AudioSource
    track: rtc.LocalAudioTrack
    sid: str
    pacer: PacedPcmSource
    client_ready: asyncio.Event = field(default_factory=asyncio.Event)
    source_closed: bool = False


class ResponseAudioTracks:
    def __init__(self, room: rtc.Room, *, sample_rate: int = 48_000, channels: int = 1,
                 ready_timeout_seconds: float = 3) -> None:
        if not math.isfinite(ready_timeout_seconds) or ready_timeout_seconds <= 0:
            raise ValueError("audio readiness timeout must be positive")
        self._ready_timeout = ready_timeout_seconds
        self._pending_ready_sid: str | None = None
        self._room = room
        self._sample_rate = sample_rate
        self._channels = channels
        self._current: _ResponseTrack | None = None
        self._requested_response_id: str | None = None
        self._stopped = False
        self._closed = False
        self._stopped_response_ids: set[str] = set()
        self._lock = asyncio.Lock()
        self._observe: Callable[[str, str], None] = lambda _name, _response_id: None

    def set_observer(self, observe: Callable[[str, str], None]) -> None:
        if self._requested_response_id is not None:
            raise RuntimeError("audio observer must be attached before the first response")
        self._observe = observe

    async def begin_response(self, response_id: str) -> None:
        if str(UUID(response_id)) != response_id:
            raise ValueError("response track requires a canonical UUID")
        async with self._lock:
            if response_id in self._stopped_response_ids:
                raise asyncio.CancelledError("response audio was already stopped")
            if self._closed:
                raise RuntimeError("response audio is closed")
            if self._current is not None and self._current.response_id == response_id:
                if self._stopped:
                    raise asyncio.CancelledError("response audio was stopped")
                return
            self._requested_response_id = response_id
            self._pending_ready_sid = None
            self._stopped = False
            previous = self._current
            if previous is not None:
                if not previous.source_closed:
                    previous.pacer.stop()
                    previous.source.clear_queue()
                previous.track.mute()
                await self._unpublish_previous(previous.sid)
                self._observe("response_audio_track_unpublished", previous.response_id)
                await self._close_source(previous)
                self._current = None
            if self._closed or self._stopped:
                raise asyncio.CancelledError("response audio was stopped while retiring the previous track")
            rtc_module = importlib.import_module("livekit.rtc")
            source: rtc.AudioSource = rtc_module.AudioSource(self._sample_rate, self._channels, queue_size_ms=0)
            try:
                track: rtc.LocalAudioTrack = rtc_module.LocalAudioTrack.create_audio_track(
                    TRACK_NAME_PREFIX + response_id, source,
                )
                publication = await self._room.local_participant.publish_track(
                    track, rtc_module.TrackPublishOptions(source=rtc_module.TrackSource.SOURCE_MICROPHONE, dtx=False),
                )
                self._current = _ResponseTrack(response_id, source, track, publication.sid,
                    PacedPcmSource(source, sample_rate=self._sample_rate, channels=self._channels))
                self._observe("response_audio_track_published", response_id)
                if self._pending_ready_sid is not None:
                    self.confirm_ready(response_id, self._pending_ready_sid)
                    self._pending_ready_sid = None
                if self._closed or self._stopped:
                    source.clear_queue()
                    track.mute()
                    raise asyncio.CancelledError("response audio was stopped while publishing")
            except BaseException:
                if self._current is not None and self._current.source is source:
                    await self._close_source(self._current)
                else:
                    await source.aclose()
                raise

    def confirm_ready(self, response_id: str, track_sid: str) -> bool:
        if self._closed or self._stopped or response_id != self._requested_response_id:
            return False
        current = self._current
        if current is None or current.response_id != response_id:
            # publish_trackの完了より早く届いた通知は1件だけ保持し、実SIDで照合する。
            self._pending_ready_sid = track_sid
            return False
        if current.sid != track_sid or current.source_closed:
            return False
        if not current.client_ready.is_set():
            current.client_ready.set()
            self._observe("response_audio_track_ready", response_id)
        return True

    async def publish(self, pcm: bytes, *, response_id: str) -> int | None:
        async with self._lock:
            current = self._current
            if self._closed or self._stopped or current is None or current.source_closed or current.response_id != response_id:
                raise asyncio.CancelledError("audio does not belong to the active response")
            if not pcm or len(pcm) % (2 * self._channels):
                raise ValueError("response audio requires complete PCM16 samples")
            try:
                await asyncio.wait_for(current.client_ready.wait(), self._ready_timeout)
                if self._stopped or self._closed:
                    raise asyncio.CancelledError("response audio stopped while waiting for readiness")
                first_capture_ns = await current.pacer.publish(pcm)
            except (asyncio.CancelledError, TimeoutError):
                self.clear(response_id)
                raise
            if self._closed or self._stopped:
                raise asyncio.CancelledError("response audio was stopped while capturing")
            return first_capture_ns

    async def finish_response(self, response_id: str) -> int | None:
        async with self._lock:
            current = self._current
            if self._closed or self._stopped or current is None or current.source_closed or current.response_id != response_id:
                raise asyncio.CancelledError("audio does not belong to the active response")
            try:
                first_capture_ns = await current.pacer.finish()
            except asyncio.CancelledError:
                self.clear(response_id)
                raise
            self._observe("response_audio_source_drained", response_id)
            return first_capture_ns

    def statistics(self, response_id: str) -> dict[str, int]:
        current = self._current
        if current is None or current.response_id != response_id:
            raise ValueError("audio statistics do not belong to the active response")
        pacer = current.pacer
        return {
            "response_audio_input_samples": pacer.input_sample_count,
            "response_audio_captured_samples": pacer.captured_sample_count,
            "response_audio_padding_samples": pacer.padding_sample_count,
            "response_audio_max_queued_samples": pacer.max_queued_samples,
            "response_audio_capture_wait_ns": pacer.capture_wait_ns,
            "response_audio_maximum_capture_wait_ns": pacer.maximum_capture_wait_ns,
            "response_audio_input_wait_ns": pacer.input_wait_ns,
            "response_audio_schedule_reset_ns": pacer.schedule_reset_ns,
        }

    def clear(self, response_id: str | None = None) -> None:
        # clearはPCM queueの空き待ち中にも即時実行できる。
        if response_id is not None and response_id != self._requested_response_id:
            return
        if self._stopped:
            return
        self._stopped = True
        if self._current is not None:
            self._current.client_ready.set()
            if not self._current.source_closed:
                self._current.pacer.stop()
                self._current.source.clear_queue()
            self._current.track.mute()

    async def stop_response(self, response_id: str) -> None:
        # まだpublishに入っていない開始処理も、後からこの応答を出力させない。
        self._stopped_response_ids.add(response_id)
        self.clear(response_id)
        async with self._lock:
            current = self._current
            if current is None or current.response_id != response_id:
                return
            if not current.source_closed:
                await current.pacer.aclose()
                # 取消を吸収したcapture_frameがqueueへ渡した分も、pump終了後に除去する。
                current.source.clear_queue()
            current.track.mute()
            self._observe("response_audio_source_stopped", response_id)
            # browserの確認前にunsubscribe/disposeを誘発しないようtrackを保持する。

    async def aclose(self) -> None:
        self._closed = True
        self.clear()
        async with self._lock:
            current = self._current
            if current is not None:
                # session cleanupがroomを切断・削除する。ここではnative sourceを解放する。
                await self._close_source(current)
                self._current = None

    async def _unpublish_previous(self, sid: str) -> None:
        participant = self._room.local_participant
        # 再接続で既に消えた所有trackへ、再度unpublishを送らない。
        if sid not in participant.track_publications:
            return
        rtc_module = importlib.import_module("livekit.rtc")
        try:
            await participant.unpublish_track(sid)
        except rtc_module.UnpublishTrackError:
            # SDKのlocal_track_unpublished通知とFFI応答は競合する。
            # 所有SIDの消失を確認できた場合だけ、sourceの解放へ進む。
            if sid in participant.track_publications:
                raise

    async def _close_source(self, current: _ResponseTrack) -> None:
        if not current.source_closed:
            await current.pacer.aclose()
            await current.source.aclose()
            current.source_closed = True
            self._observe("response_audio_source_closed", current.response_id)

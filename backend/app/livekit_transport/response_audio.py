"""応答ごとに独立したAudioSourceとAudioTrackを所有する。"""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    import livekit.rtc as rtc

TRACK_NAME_PREFIX = "ds-response-v1:"


@dataclass
class _ResponseTrack:
    response_id: str
    source: rtc.AudioSource
    track: rtc.LocalAudioTrack
    sid: str
    source_closed: bool = False


class ResponseAudioTracks:
    def __init__(self, room: rtc.Room, *, sample_rate: int = 48_000, channels: int = 1) -> None:
        self._room = room
        self._sample_rate = sample_rate
        self._channels = channels
        self._current: _ResponseTrack | None = None
        self._requested_response_id: str | None = None
        self._stopped = False
        self._closed = False
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
            if self._closed:
                raise RuntimeError("response audio is closed")
            if self._current is not None and self._current.response_id == response_id:
                if self._stopped:
                    raise asyncio.CancelledError("response audio was stopped")
                return
            self._requested_response_id = response_id
            self._stopped = False
            previous = self._current
            if previous is not None:
                if not previous.source_closed:
                    previous.source.clear_queue()
                previous.track.mute()
                await self._room.local_participant.unpublish_track(previous.sid)
                self._observe("response_audio_track_unpublished", previous.response_id)
                await self._close_source(previous)
                self._current = None
            if self._closed or self._stopped:
                raise asyncio.CancelledError("response audio was stopped while retiring the previous track")
            rtc_module = importlib.import_module("livekit.rtc")
            source: rtc.AudioSource = rtc_module.AudioSource(self._sample_rate, self._channels)
            try:
                track: rtc.LocalAudioTrack = rtc_module.LocalAudioTrack.create_audio_track(
                    TRACK_NAME_PREFIX + response_id, source,
                )
                publication = await self._room.local_participant.publish_track(
                    track, rtc_module.TrackPublishOptions(source=rtc_module.TrackSource.SOURCE_MICROPHONE),
                )
                self._current = _ResponseTrack(response_id, source, track, publication.sid)
                self._observe("response_audio_track_published", response_id)
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

    async def publish(self, pcm: bytes, *, response_id: str) -> None:
        async with self._lock:
            current = self._current
            if self._closed or self._stopped or current is None or current.source_closed or current.response_id != response_id:
                raise asyncio.CancelledError("audio does not belong to the active response")
            if not pcm or len(pcm) % (2 * self._channels):
                raise ValueError("response audio requires complete PCM16 samples")
            rtc_module = importlib.import_module("livekit.rtc")
            await current.source.capture_frame(rtc_module.AudioFrame(
                pcm, self._sample_rate, self._channels, len(pcm) // (2 * self._channels),
            ))
            if self._closed or self._stopped:
                raise asyncio.CancelledError("response audio was stopped while capturing")

    def clear(self, response_id: str | None = None) -> None:
        # clearはcapture_frameのbackpressure待ち中にも即時実行できる。
        if response_id is not None and response_id != self._requested_response_id:
            return
        self._stopped = True
        if self._current is not None:
            if not self._current.source_closed:
                self._current.source.clear_queue()
            self._current.track.mute()

    async def aclose(self) -> None:
        self._closed = True
        self.clear()
        async with self._lock:
            current = self._current
            if current is not None:
                # session cleanupがroomを切断・削除する。ここではnative sourceを解放する。
                await self._close_source(current)
                self._current = None

    async def _close_source(self, current: _ResponseTrack) -> None:
        if not current.source_closed:
            await current.source.aclose()
            current.source_closed = True
            self._observe("response_audio_source_closed", current.response_id)

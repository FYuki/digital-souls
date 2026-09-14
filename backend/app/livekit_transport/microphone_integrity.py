"""受信統計の非無音concealmentを、有界のmedia観測区間として扱う。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from livekit.rtc._proto.stats_pb2 import RtcStats
from app.voice_input.pipeline import AudioInputFault

POLL_SECONDS = 0.1
VERIFY_TIMEOUT_SECONDS = 1.0
MAX_OBSERVATION_WINDOWS = 512


@dataclass(frozen=True)
class _Counters:
    identifier: str
    timestamp: int
    concealed: int
    silent: int


def _counters(stats: Sequence[RtcStats]) -> _Counters:
    inbound = [
        item.inbound_rtp
        for item in stats
        if item.HasField("inbound_rtp") and item.inbound_rtp.stream.kind == "audio"
    ]
    if len(inbound) != 1:
        raise AudioInputFault("audio_integrity_unavailable")
    item = inbound[0]
    codecs = [
        entry.codec.codec
        for entry in stats
        if entry.HasField("codec") and entry.codec.rtc.id == item.stream.codec_id
    ]
    # 現行ブラウザのOpusは48kHzの受信sample統計。16kHzへ正規化したVAD PCMと混同しない。
    if (
        len(codecs) != 1
        or codecs[0].mime_type.lower() != "audio/opus"
        or codecs[0].clock_rate != 48000
        or not item.inbound.HasField("concealed_samples")
        or not item.inbound.HasField("silent_concealed_samples")
        or not item.rtc.HasField("timestamp")
        or not item.rtc.id
        or item.inbound.silent_concealed_samples > item.inbound.concealed_samples
    ):
        raise AudioInputFault("audio_integrity_unavailable")
    return _Counters(
        item.rtc.id,
        item.rtc.timestamp,
        item.inbound.concealed_samples,
        item.inbound.silent_concealed_samples,
    )


class MicrophoneIntegrity:
    """開始時に欠測なら、その区間を正常とせず、対象発話を保留後に破棄する。"""

    def __init__(
        self,
        get_stats: Callable[[], Awaitable[Sequence[RtcStats]]],
        source_position: Callable[[], int],
    ) -> None:
        self._get_stats = get_stats
        self._position = source_position
        self._previous: _Counters | None = None
        self._known_start: int | None = None
        self._covered_end = 0
        self._faults: deque[tuple[int, int, str]] = deque()
        self._lock = asyncio.Lock()
        self._closed = False
        self._ready = asyncio.Event()

    async def observe(self) -> None:
        async with self._lock:
            if self._closed:
                raise AudioInputFault("audio_integrity_unavailable")
            # 応答を待つ間に届いたPCMまで統計確認済みとしない。
            position = self._position()
            try:
                async with asyncio.timeout(VERIFY_TIMEOUT_SECONDS):
                    current = _counters(await self._get_stats())
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise AudioInputFault("audio_integrity_unavailable") from error
            if self._closed:
                raise AudioInputFault("audio_integrity_unavailable")
            previous = self._previous
            if previous is None:
                self._known_start = position
            else:
                if (
                    current.identifier != previous.identifier
                    or current.timestamp <= previous.timestamp
                    or current.concealed < previous.concealed
                    or current.silent < previous.silent
                ):
                    raise AudioInputFault("audio_integrity_unavailable")
                missing = (current.concealed - previous.concealed) - (
                    current.silent - previous.silent
                )
                if missing < 0:
                    raise AudioInputFault("audio_integrity_unavailable")
                if missing >= 48000 * 80 // 1000:
                    self._faults.append((self._covered_end, position, "audio_gap"))
            self._previous = current
            self._covered_end = position
            self._ready.set()
            # 30秒発話＋prerollより長い保持窓。超過区間は確認不能として扱う。
            while len(self._faults) > MAX_OBSERVATION_WINDOWS:
                _, end, _ = self._faults.popleft()
                self._known_start = max(self._known_start or 0, end + 1)

    async def wait_ready(self) -> None:
        """入力認可前に最初の有効統計を待つ。上限は呼出側の認可deadlineで管理する。"""
        await self._ready.wait()
        if self._closed or self._previous is None or self._known_start is None:
            raise AudioInputFault("audio_integrity_unavailable")

    async def run(self) -> None:
        while not self._closed:
            try:
                await self.observe()
            except AudioInputFault:
                pass  # 発話確定時はverifyで未観測を拒否する。
            await asyncio.sleep(POLL_SECONDS)

    def _verified_range(self, start_sample: int, end_sample: int) -> bool:
        if self._closed:
            raise AudioInputFault("audio_integrity_unavailable")
        if (
            self._known_start is None
            or self._known_start > start_sample
            or self._covered_end < end_sample
        ):
            return False
        for lower, upper, reason in self._faults:
            if lower <= end_sample and upper >= start_sample:
                raise AudioInputFault(reason)
        return True

    async def verify(self, start_sample: int, end_sample: int) -> None:
        if start_sample < 0 or end_sample < start_sample:
            raise AudioInputFault("audio_integrity_unavailable")
        try:
            async with asyncio.timeout(VERIFY_TIMEOUT_SECONDS):
                while True:
                    # background pollで既に確認した範囲は、その証拠で判定する。
                    # 同じ統計snapshotの再読込を待って確認済み区間を欠測にしない。
                    if self._verified_range(start_sample, end_sample):
                        return
                    try:
                        await self.observe()
                    except AudioInputFault as error:
                        if error.code == "audio_gap":
                            raise
                    else:
                        if self._verified_range(start_sample, end_sample):
                            return
                    await asyncio.sleep(POLL_SECONDS)
        except TimeoutError as error:
            raise AudioInputFault("audio_integrity_unavailable") from error

    def close(self) -> None:
        self._closed = True
        self._ready.set()
        self._faults.clear()

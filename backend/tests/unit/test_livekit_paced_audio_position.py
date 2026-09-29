"""PacedPcmSource.sent_sample_count(at_ns) を検証する。

capture_frame完了時刻と累積sample数の記録から、過去任意時点のBE送出量を引く。
livekit rtc不在ではpumpが動かないため skip する。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.livekit_transport.paced_audio import PacedPcmSource


class _FakeCapture:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.interrupted = False

    async def capture_frame(self, frame) -> None:
        self.chunks.append(bytes(frame.data))

    async def aclose(self, *, interrupted: bool = False) -> None:
        self.interrupted = interrupted


def _frame(pcm: bytes):
    class Frame:
        data = pcm
        sample_rate = 48000
        samples_per_channel = len(pcm) // 4
        num_channels = 1
    return Frame()


async def _wait_captured(player: PacedPcmSource, samples: int) -> None:
    """pumpがcapture完了するまで待つ（livekit rtc不在では即失敗する環境ではskip）。"""
    for _ in range(200):
        if player.captured_sample_count >= samples:
            return
        if player._error is not None:
            pytest.skip("livekit rtc が無い環境では capture pump が動かない")
        await asyncio.sleep(0.005)
    raise AssertionError("capture pump が時間内に進まなかった")


def test_sent_sample_count_uses_capture_completion_log():
    """完了したcaptureは全て送出済みと数える。"""
    async def exercise():
        player = PacedPcmSource(_FakeCapture())
        pcm = b"a" * (960 * 2)
        await player.publish(pcm)
        await player.publish(pcm)
        await _wait_captured(player, 1920)
        assert player.sent_sample_count(time.monotonic_ns() + 10_000_000) == 1920
        await player.aclose()
    asyncio.run(exercise())


def test_sent_sample_count_interpolates_from_capture_log():
    async def exercise():
        player = PacedPcmSource(_FakeCapture())
        # frameは480sample。2回publishで計4 frame=1920sample送出される。
        pcm = b"a" * (960 * 2)
        await player.publish(pcm)
        await player.publish(pcm)
        await _wait_captured(player, 1920)

        log = player._capture_log
        assert len(log) == 4
        first_end_ns, first_total = log[0]
        assert first_total == 480
        # 1本目の書き込み完了時点では先頭分が送出済み（先頭含む=bisect_right）。
        assert player.sent_sample_count(first_end_ns) == 480
        # 1本目完了の1ns前: まだ送出されていない。
        assert player.sent_sample_count(first_end_ns - 1) == 0
        # 途中境界: 2本目完了時点では960、最終では1920。
        mid_end_ns, mid_total = log[1]
        assert player.sent_sample_count(mid_end_ns) == mid_total == 960
        last_end_ns, last_total = log[-1]
        assert player.sent_sample_count(last_end_ns) == last_total == 1920
        await player.aclose()
    asyncio.run(exercise())


def test_sent_sample_count_counts_future_as_everything_sent():
    async def exercise():
        player = PacedPcmSource(_FakeCapture())
        await player.publish(b"a" * (480 * 2))
        await _wait_captured(player, 480)
        assert player.sent_sample_count(time.monotonic_ns() + 10**9) == 480
        await player.aclose()
    asyncio.run(exercise())

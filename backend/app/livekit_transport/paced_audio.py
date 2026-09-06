"""入力前の無音を作らず、上限付きPCM queueから10msずつnative sourceへ渡す。"""
from __future__ import annotations

import asyncio
import importlib
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import livekit.rtc as rtc


class PacedPcmSource:
    def __init__(self, source: rtc.AudioSource, *, sample_rate: int = 48000,
                 channels: int = 1, buffer_ms: int = 1000) -> None:
        if sample_rate <= 0 or sample_rate % 100 or channels < 1 or buffer_ms < 20 or buffer_ms % 10:
            raise ValueError("paced audio requires 10ms frames and at least 20ms of buffering")
        self._source = source
        self._rate, self._channels = sample_rate, channels
        self._frame_samples = sample_rate // 100
        self._frame_bytes = self._frame_samples * channels * 2
        self._capacity = self._frame_bytes * (buffer_ms // 10)
        self._buffer = bytearray()
        self._available, self._space, self._first_capture = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self._space.set()
        self._stopped = False
        self._finished = False
        self._error: Exception | None = None
        self.first_capture_ns: int | None = None
        self.input_sample_count = 0
        self.captured_sample_count = 0
        self.padding_sample_count = 0
        self.max_queued_samples = 0
        self._task = asyncio.create_task(self._pump())

    @property
    def queued_samples(self) -> int:
        return len(self._buffer) // (self._channels * 2)

    def _check(self) -> None:
        if self._error is not None:
            raise RuntimeError("paced audio source failed") from self._error
        if self._stopped:
            raise asyncio.CancelledError("paced audio was stopped")

    async def publish(self, pcm: bytes) -> int | None:
        self._check()
        if self._finished:
            raise ValueError("response audio input is already finished")
        if not pcm or len(pcm) % (2 * self._channels):
            raise ValueError("response audio requires complete PCM16 samples")
        await self._append(pcm)
        self.input_sample_count += len(pcm) // (2 * self._channels)
        # 10ms未満は次segmentと連結する。末尾のflush前に架空の送信時刻を返さない。
        if self.input_sample_count >= self._frame_samples:
            await self._first_capture.wait()
            self._check()
        return self.first_capture_ns

    async def _append(self, pcm: bytes) -> None:
        cursor = 0
        while cursor < len(pcm):
            self._check()
            remaining = self._capacity - len(self._buffer)
            if remaining == 0:
                self._space.clear()
                await self._space.wait()
                continue
            count = min(remaining, len(pcm) - cursor)
            self._buffer.extend(pcm[cursor:cursor + count])
            self.max_queued_samples = max(self.max_queued_samples, self.queued_samples)
            cursor += count
            if len(self._buffer) >= self._frame_bytes:
                self._available.set()

    async def finish(self) -> int | None:
        self._check()
        if not self._finished:
            if self.input_sample_count:
                # 20ms packetの末尾と追加20msのcodec tailを明示的に送る。
                # paddingはlogical PCM sample数へ含めず、source offset一致の証拠にも使わない。
                packet_samples = self._frame_samples * 2
                self.padding_sample_count = (-self.input_sample_count) % packet_samples + packet_samples
                await self._append(bytes(self.padding_sample_count * self._channels * 2))
            self._finished = True
            self._available.set()
        await asyncio.shield(self._task)
        self._check()
        return self.first_capture_ns

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._buffer.clear()
        self._space.set()
        self._available.set()
        self._first_capture.set()
        self._task.cancel()

    async def aclose(self) -> None:
        self.stop()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _pump(self) -> None:
        deadline: float | None = None
        try:
            rtc_module = importlib.import_module("livekit.rtc")
            loop = asyncio.get_running_loop()
            while not self._stopped:
                if len(self._buffer) < self._frame_bytes:
                    if self._finished:
                        return
                    self._available.clear()
                    await self._available.wait()
                    deadline = None
                    continue
                if deadline is not None:
                    await asyncio.sleep(max(0, deadline - loop.time()))
                if self._stopped:
                    return
                pcm = bytes(self._buffer[:self._frame_bytes])
                del self._buffer[:self._frame_bytes]
                self._space.set()
                await asyncio.wait_for(self._source.capture_frame(rtc_module.AudioFrame(
                    pcm, self._rate, self._channels, self._frame_samples,
                )), 5)
                self.captured_sample_count += self._frame_samples
                if self.first_capture_ns is None:
                    self.first_capture_ns = time.monotonic_ns()
                    self._first_capture.set()
                # 通常は10ms周期を保ち、長い停止後に過去の全frameを一度に送らない。
                deadline = max((deadline if deadline is not None else loop.time()) + .01, loop.time())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._error = error
            self._buffer.clear()
        finally:
            self._space.set()
            self._first_capture.set()

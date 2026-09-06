from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from app.livekit_transport.paced_audio import PacedPcmSource


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setitem(sys.modules, "livekit.rtc", SimpleNamespace(
        AudioFrame=lambda pcm, rate, channels, samples: SimpleNamespace(data=pcm, samples=samples),
    ))
    class Source:
        def __init__(self):
            self.frames = []
            self.entered = asyncio.Event()
            self.release = None
            self.error = False

        async def capture_frame(self, frame):
            self.entered.set()
            if self.error:
                raise RuntimeError("native capture failed")
            if self.release is not None:
                await self.release.wait()
            self.frames.append(frame)
    return Source


def test_sends_nothing_before_input_and_preserves_samples_across_segments(source):
    async def exercise():
        native = source()
        output = PacedPcmSource(native)
        await asyncio.sleep(.025)
        assert native.frames == []
        assert await output.publish(b"\x01\x00" * 100) is None
        await asyncio.sleep(.015)
        assert native.frames == []
        first = await output.publish(b"\x02\x00" * 380)
        assert first is not None
        assert native.frames[0].data == b"\x01\x00" * 100 + b"\x02\x00" * 380
        assert await output.finish() == first
        assert output.input_sample_count == 480
        assert output.padding_sample_count == 1440
        assert output.captured_sample_count == 1920
        assert all(frame.samples == 480 for frame in native.frames)
        assert b"".join(frame.data for frame in native.frames[1:]) == bytes(1440 * 2)
        assert await output.finish() == first
        with pytest.raises(ValueError):
            await output.publish(bytes(960))
        await output.aclose()
    asyncio.run(exercise())


def test_bounded_queue_backpressure_and_stop_release_waiting_producer(source):
    async def exercise():
        native = source()
        native.release = asyncio.Event()
        output = PacedPcmSource(native, buffer_ms=20)
        publish = asyncio.create_task(output.publish(b"\x01\x00" * 2400))
        await asyncio.wait_for(native.entered.wait(), .5)
        await asyncio.sleep(0)
        assert not publish.done()
        assert output.queued_samples <= 960
        output.stop()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(publish, .5)
        await output.aclose()
        assert output.queued_samples == 0
        assert native.frames == []
    asyncio.run(exercise())


def test_native_failure_is_returned_to_producer_and_finish(source):
    async def exercise():
        native = source()
        native.error = True
        output = PacedPcmSource(native, buffer_ms=20)
        with pytest.raises(RuntimeError, match="paced audio source failed"):
            await asyncio.wait_for(output.publish(bytes(960 * 5)), .5)
        with pytest.raises(RuntimeError, match="paced audio source failed"):
            await output.finish()
        await output.aclose()
    asyncio.run(exercise())


def test_short_final_segment_is_sent_only_on_finish(source):
    async def exercise():
        native = source()
        output = PacedPcmSource(native)
        assert await output.publish(b"\x04\x00") is None
        assert await output.finish() is not None
        assert output.input_sample_count == 1
        assert output.padding_sample_count == 1919
        assert b"".join(frame.data for frame in native.frames) == b"\x04\x00" + bytes(1919 * 2)
        await output.aclose()
    asyncio.run(exercise())


def test_empty_response_does_not_generate_tail_audio(source):
    async def exercise():
        native = source()
        output = PacedPcmSource(native)
        assert await output.finish() is None
        assert native.frames == []
        await output.aclose()
        await output.aclose()
    asyncio.run(exercise())

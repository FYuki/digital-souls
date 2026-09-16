"""実SDKのframe・queueでsample位置を付ける境界と欠落を検証する。"""

from __future__ import annotations

import asyncio
import pytest
from livekit.rtc import AudioFrame
from livekit.rtc._utils import RingQueue

from app.livekit_transport.microphone_frames import MicrophoneFrameClock
from app.voice_input.pipeline import AudioInputFault, VoiceInputPipeline


def frame() -> AudioFrame:
    return AudioFrame(bytes(320), 16000, 1, 160)


def test_queue_drop_keeps_original_sample_position_and_pipeline_rejects_gap() -> None:
    async def scenario() -> None:
        clock = MicrophoneFrameClock()
        queue: RingQueue[AudioFrame] = RingQueue(capacity=2)
        pipeline = VoiceInputPipeline()
        try:
            queue.put(clock._process(frame()))
            pcm, start = clock.read(await queue.get())
            assert start == 0
            pipeline.feed(pcm, start_sample=start)
            for _ in range(3):
                queue.put(clock._process(frame()))
            pcm, start = clock.read(await queue.get())
            assert start == 320  # sample 160のframeがSDK queueから落ちている。
            with pytest.raises(AudioInputFault, match="audio_gap"):
                pipeline.feed(pcm, start_sample=start)
            pcm, start = clock.read(await queue.get())
            assert start == 480
            assert pipeline.feed(pcm, start_sample=start) == ()
        finally:
            pipeline.close()
            clock._close()

    asyncio.run(scenario())


def test_missing_or_other_track_stamp_is_not_accepted() -> None:
    first, second = MicrophoneFrameClock(), MicrophoneFrameClock()
    with pytest.raises(AudioInputFault, match="microphone_position_unavailable"):
        first.read(frame())
    stamped = first._process(frame())
    assert first.read(stamped)[1] == 0
    with pytest.raises(AudioInputFault, match="microphone_position_unavailable"):
        second.read(stamped)
    first._close()
    with pytest.raises(AudioInputFault, match="microphone_clock_unavailable"):
        first.read(stamped)


@pytest.mark.parametrize(
    "audio",
    [
        AudioFrame(bytes(320), 48000, 1, 160),
        AudioFrame(bytes(640), 16000, 2, 160),
        AudioFrame(bytes(640), 16000, 1, 320),
    ],
)
def test_invalid_source_format_is_rejected(audio: AudioFrame) -> None:
    clock = MicrophoneFrameClock()
    with pytest.raises(AudioInputFault, match="invalid_audio_frame"):
        clock._process(audio)


def test_frame_arrival_delay_does_not_advance_media_clock() -> None:
    async def scenario() -> None:
        clock = MicrophoneFrameClock()
        first = clock._process(frame())
        await asyncio.sleep(0)
        second = clock._process(frame())
        assert clock.read(first)[1] == 0
        assert clock.read(second)[1] == 160

    asyncio.run(scenario())

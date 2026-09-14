"""SDK queue投入前のsample位置を保持し、ローカルframe欠落を検出可能にする。"""

from __future__ import annotations

from dataclasses import dataclass

from livekit.rtc import AudioFrame
from livekit.rtc.frame_processor import FrameProcessor

from app.voice_input.pipeline import AudioInputFault

MICROPHONE_SAMPLE_RATE = 16000
MICROPHONE_FRAME_MS = 10
MICROPHONE_QUEUE_CAPACITY = 160
_FRAME_KEY = "digital-souls.microphone-position"


@dataclass(frozen=True)
class _Position:
    owner: object
    start_sample: int
    sample_count: int


class MicrophoneFrameClock(FrameProcessor[AudioFrame]):
    """受信mediaのsample時計。送信側RTP timestampや到着時計の代用品ではない。"""

    def __init__(self) -> None:
        self._owner = object()
        self._next_sample = 0
        self._enabled = True
        self._closed = False

    @property
    def samples_seen(self) -> int:
        return self._next_sample

    @property
    def enabled(self) -> bool:
        return self._enabled and not self._closed

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @staticmethod
    def _validate(frame: AudioFrame) -> None:
        if (
            frame.sample_rate != MICROPHONE_SAMPLE_RATE
            or frame.num_channels != 1
            or not 0
            < frame.samples_per_channel
            <= MICROPHONE_SAMPLE_RATE * MICROPHONE_FRAME_MS // 1000
            or frame.data.nbytes != frame.samples_per_channel * 2
        ):
            raise AudioInputFault("invalid_audio_frame")

    def _process(self, frame: AudioFrame) -> AudioFrame:
        if not self.enabled:
            raise AudioInputFault("microphone_clock_unavailable")
        self._validate(frame)
        frame.userdata[_FRAME_KEY] = _Position(
            self._owner, self._next_sample, frame.samples_per_channel
        )
        self._next_sample += frame.samples_per_channel
        return frame

    def read(self, frame: AudioFrame) -> tuple[bytes, int]:
        """欠落差分は上位の連続PCM pipelineで検証する。別trackの印は受け付けない。"""
        if self._closed:
            raise AudioInputFault("microphone_clock_unavailable")
        self._validate(frame)
        position = frame.userdata.get(_FRAME_KEY)
        if (
            not isinstance(position, _Position)
            or position.owner is not self._owner
            or position.sample_count != frame.samples_per_channel
        ):
            # SDKはprocessor例外時に元frameを流すため、印なしを正常入力にしない。
            raise AudioInputFault("microphone_position_unavailable")
        return bytes(frame.data), position.start_sample

    def _close(self) -> None:
        self._closed = True

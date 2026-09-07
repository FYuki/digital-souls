"""16kHz PCM16の長いほぼ無音のprerollを、語頭の余白を残して短くする。"""
from __future__ import annotations

import struct

# Opus復号後の量子化雑音を想定した約-66dBFS。VADの発話判定閾値には使わない。
_QUIET_PEAK_PCM16 = 16
_PREFIX_SAMPLES = 16_000 * 320 // 1000


def prepare_stt_audio(pcm: bytes) -> tuple[bytes, int]:
    """最初の閾値超過より前の320msと、以降の全sampleをそのまま保持する。"""
    if len(pcm) % 2:
        raise ValueError("STT input must contain complete PCM16 samples")
    for index, (sample,) in enumerate(struct.iter_unpack("<h", pcm)):
        if abs(sample) > _QUIET_PEAK_PCM16:
            removed = max(0, index - _PREFIX_SAMPLES)
            return pcm[removed * 2:], removed
    # 全体が小音量の場合は発話か無音かをここで決めず、元の入力を維持する。
    return pcm, 0


class SttSignalSpan:
    """追記されるPCMの最初の静音閾値超過以降の長さを、重複走査せず数える。"""

    def __init__(self) -> None:
        self._scanned_samples = 0
        self._first_signal_sample: int | None = None

    def sample_count(self, pcm: bytes | bytearray) -> int:
        samples = len(pcm) // 2
        if len(pcm) % 2 or samples < self._scanned_samples:
            raise ValueError("STT signal input must be append-only complete PCM16")
        if self._first_signal_sample is None:
            for offset, (value,) in enumerate(
                struct.iter_unpack("<h", memoryview(pcm)[self._scanned_samples * 2:])
            ):
                if abs(value) > _QUIET_PEAK_PCM16:
                    self._first_signal_sample = self._scanned_samples + offset
                    break
        self._scanned_samples = samples
        if self._first_signal_sample is None:
            return 0
        return samples - self._first_signal_sample

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

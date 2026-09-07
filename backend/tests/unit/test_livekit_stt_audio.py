from __future__ import annotations

import struct

import pytest

from app.livekit_transport.stt_audio import prepare_stt_audio


def pcm(values: list[int]) -> bytes:
    return struct.pack(f"<{len(values)}h", *values)


def test_long_preroll_keeps_onset_margin_and_all_following_audio():
    quiet = [-1, 0, 16, -16] * 8000
    speech_and_pause = [17, -17, 3000, -3000] + [0] * 9600 + [2000, -2000] + [0] * 16000
    original = pcm(quiet + speech_and_pause)
    prepared, removed = prepare_stt_audio(original)
    assert removed == 32000 - 5120
    assert prepared == original[removed * 2:]
    assert prepared == pcm(quiet[-5120:] + speech_and_pause)


@pytest.mark.parametrize('values', [[], [0] * 40000, [16, -16] * 20000,
                                   [0] * 5000 + [17], [17] + [0] * 40000,
                                   [0] * 100 + [-17] + [0] * 40000])
def test_short_preroll_and_entirely_quiet_input_are_unchanged(values):
    original = pcm(values)
    assert prepare_stt_audio(original) == (original, 0)


def test_truncated_pcm_is_rejected():
    with pytest.raises(ValueError, match='complete PCM16'):
        prepare_stt_audio(b'\x00')

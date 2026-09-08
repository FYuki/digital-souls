from __future__ import annotations

from fractions import Fraction
from pathlib import Path
import sys
import wave

import av
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/voice_quality'))
try:
    from pcm_boundary_alignment import align_pcm, match_pcm_edges, match_bounded_pcm_edges
finally:
    sys.path.remove(str(ROOT / 'scripts/voice_quality'))


def pcm(samples):
    return np.asarray(samples, dtype='<i2').tobytes()


@pytest.fixture
def signal():
    # 再現可能な異なる周波数・振幅のsignal。音声fixtureの代替受入には数えない。
    rng = np.random.default_rng(150)
    x = rng.integers(-8000, 8000, 12800, dtype=np.int16)
    return np.r_[np.zeros(3200, dtype=np.int16), x, np.zeros(6400, dtype=np.int16)]


def test_leading_and_trailing_sample_loss_is_not_replaced_by_vad_timing(signal):
    reference = pcm(signal)
    captured = pcm(signal[3200 + 1920:16000 - 1920])
    result = align_pcm(reference, captured, speech_start_sample=3200, speech_end_sample=16000)
    # anchorが切れた場合も未確認を0損失にしない。
    assert result['status'] == 'unverified' or (
        result['leading_loss_upper_ms'] > 100 and result['early_end_loss_upper_ms'] > 100)


def test_gain_offset_and_padding_keep_independent_anchor_positions(signal):
    captured = np.r_[np.zeros(1600), signal.astype(float) * .5 + 100, np.zeros(320)]
    result = align_pcm(pcm(signal), pcm(captured), speech_start_sample=3200, speech_end_sample=16000)
    assert result['status'] == 'aligned'
    assert result['lag_difference_samples'] == 0
    assert result['leading_loss_upper_ms'] == result['early_end_loss_upper_ms'] == 0
    assert all(a['captured_start_sample'] - a['reference_start_sample'] == 1600 for a in result['anchors'])


def test_duplicate_reference_and_silence_do_not_establish_boundary(signal):
    for captured in (np.r_[signal, signal], np.zeros(len(signal))):
        result = align_pcm(pcm(signal), pcm(captured), speech_start_sample=3200, speech_end_sample=16000)
        assert result['status'] == 'unverified'
        assert result['leading_loss_upper_ms'] is None
        assert result['early_end_loss_upper_ms'] is None


def test_displaced_second_half_cannot_establish_one_contiguous_offset(signal):
    captured = np.r_[signal[:9600], np.zeros(640), signal[9600:]]
    result = align_pcm(pcm(signal), pcm(captured), speech_start_sample=3200, speech_end_sample=16000)
    assert result['status'] == 'unverified'
    assert result['reason'] == 'inconsistent_anchor_offsets'


@pytest.mark.parametrize('start,end', [(True, 1000), (0, 999999), (-1, 900), (1000, 1000)])
def test_invalid_boundaries_are_rejected(signal, start, end):
    with pytest.raises(ValueError, match='boundaries invalid'):
        align_pcm(pcm(signal), pcm(signal), speech_start_sample=start, speech_end_sample=end)


def test_real_opus_roundtrip_of_fixed_fixture_keeps_known_alignment():
    with wave.open(str(ROOT / 'frontend/playwright/fixtures/speech.wav')) as source:
        assert source.getframerate() == 48000 and source.getnchannels() == 1
        original = np.frombuffer(source.readframes(source.getnframes()), dtype='<i2')
    encoder = av.CodecContext.create('libopus', 'w')
    encoder.sample_rate = 48000
    encoder.layout = 'mono'
    encoder.format = 's16'
    encoder.bit_rate = 32000
    encoder.time_base = Fraction(1, 48000)
    encoder.open()
    decoder = av.CodecContext.create('opus', 'r')
    decoded = []
    for index in range(0, len(original), 960):
        data = np.zeros((1, 960), dtype=np.int16)
        chunk = original[index:index + 960]
        data[0, :len(chunk)] = chunk
        frame = av.AudioFrame.from_ndarray(data, format='s16', layout='mono')
        frame.sample_rate = 48000
        frame.pts = index
        frame.time_base = Fraction(1, 48000)
        for packet in encoder.encode(frame):
            decoded.extend(f.to_ndarray()[0] for f in decoder.decode(packet))
    for packet in encoder.encode(None):
        decoded.extend(f.to_ndarray()[0] for f in decoder.decode(packet))
    captured = np.concatenate(decoded) * 32768
    reference = pcm(original[::3])
    edge = match_pcm_edges(reference, pcm(captured[::3]), speech_start_sample=5280, speech_end_sample=15040)
    assert edge['status'] == 'matched'
    bounded = match_bounded_pcm_edges(reference, pcm(captured[::3]), speech_start_sample=5280, speech_end_sample=15040)
    assert bounded['status'] == 'matched'
    result = align_pcm(reference, pcm(captured[::3]), speech_start_sample=5280, speech_end_sample=15040)
    assert result['status'] == 'aligned'
    assert result['lag_difference_samples'] == 0
    # codec固有のlookaheadを入力時間や欠落と誤認しない。
    assert all(a['captured_start_sample'] - a['reference_start_sample'] == 104 for a in result['anchors'])
    assert result['leading_loss_upper_ms'] == result['early_end_loss_upper_ms'] == 0
    for cut in (captured[::3][7200:], captured[::3][:13120]):
        assert match_pcm_edges(reference, pcm(cut), speech_start_sample=5280, speech_end_sample=15040)['status'] == 'unverified'
        result = align_pcm(reference, pcm(cut), speech_start_sample=5280, speech_end_sample=15040)
        assert result['status'] == 'unverified' or max(result['leading_loss_upper_ms'], result['early_end_loss_upper_ms']) > 100


def test_direct_edges_do_not_reinterpret_interior_displacement_as_continuity(signal):
    captured = np.r_[signal[:9600], np.zeros(640), signal[9600:]]
    uniform = align_pcm(pcm(signal), pcm(captured), speech_start_sample=3200, speech_end_sample=16000)
    edges = match_pcm_edges(pcm(signal), pcm(captured), speech_start_sample=3200, speech_end_sample=16000)
    assert uniform['status'] == 'unverified'
    assert uniform['reason'] == 'inconsistent_anchor_offsets'
    assert edges['status'] == 'matched'
    assert edges['interior_continuity_verified'] is False
    assert edges['anchors'][0]['reference_start_sample'] == 3200
    assert edges['anchors'][1]['reference_start_sample'] + edges['anchors'][1]['sample_count'] == 16000


def test_direct_edges_reject_repeated_audio_and_reordered_edges(signal):
    for captured in (np.r_[signal, signal], np.r_[signal[9600:], signal[:9600]]):
        result = match_pcm_edges(pcm(signal), pcm(captured), speech_start_sample=3200, speech_end_sample=16000)
        assert result['status'] == 'unverified'


@pytest.mark.parametrize('side', ['leading', 'trailing'])
@pytest.mark.parametrize('damage', ['silenced', 'deleted'])
def test_short_blocks_reject_101ms_loss_hidden_by_louder_rest(signal, side, damage):
    reference = signal.copy()
    reference[3200:4816] //= 100
    reference[14384:16000] //= 100
    captured = reference.copy()
    if side == 'leading':
        if damage == 'silenced': captured[:4816] = 0
        else: captured = captured[4816:]
    else:
        if damage == 'silenced': captured[14384:] = 0
        else: captured = captured[:14384]
    if damage == 'silenced':
        # 大きな残存波形が200ms窓の相関を支配する旧方式の反例。
        assert match_pcm_edges(pcm(reference), pcm(captured), speech_start_sample=3200, speech_end_sample=16000)['status'] == 'matched'
    assert match_bounded_pcm_edges(pcm(reference), pcm(reference), speech_start_sample=3200, speech_end_sample=16000)['status'] == 'matched'
    result = match_bounded_pcm_edges(pcm(reference), pcm(captured), speech_start_sample=3200, speech_end_sample=16000)
    assert result['status'] == 'unverified'
    assert not result['full_band_quality_verified']


def test_bounded_blocks_reject_ambiguous_reordered_or_silent_audio(signal):
    for captured in (np.r_[signal, signal], np.r_[signal[9600:], signal[:9600]], np.zeros(len(signal)), np.zeros(1)):
        assert match_bounded_pcm_edges(pcm(signal), pcm(captured), speech_start_sample=3200, speech_end_sample=16000)['status'] == 'unverified'


def test_local_blocks_allow_interior_shift_without_claiming_continuity(signal):
    captured = np.r_[signal[:9600], np.zeros(640), signal[9600:]]
    result = match_bounded_pcm_edges(pcm(signal), pcm(captured), speech_start_sample=3200, speech_end_sample=16000)
    assert result['status'] == 'matched'
    assert not result['interior_continuity_verified']

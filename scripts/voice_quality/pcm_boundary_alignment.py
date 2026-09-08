"""16kHz PCM16の合成fixtureを実入力へ対応付ける診断。音声・本文は返さない。"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16_000
MIN_CORRELATION = .8
MIN_PEAK_MARGIN = .1
MAX_LAG_DIFFERENCE_SAMPLES = 32
ALIGNMENT_TOLERANCE_SAMPLES = 32


def _samples(pcm: bytes) -> np.ndarray:
    if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
        raise ValueError('nonempty PCM16 samples required')
    return np.frombuffer(pcm, dtype='<i2').astype(np.float64)


def _anchor(reference: np.ndarray, low: int, high: int, width: int) -> tuple[int, np.ndarray]:
    energy = np.convolve(reference[low:high] ** 2, np.ones(width), 'valid')
    start = low + int(np.argmax(energy))
    return start, reference[start:start + width]


def _match(captured: np.ndarray, position: int, reference: np.ndarray) -> dict:
    width = len(reference)
    result = {'reference_start_sample': position, 'sample_count': width,
              'captured_start_sample': None, 'correlation': None, 'competing_peak_correlation': None,
              'accepted': False}
    if len(captured) < width:
        return result
    centered = reference - reference.mean()
    reference_energy = float(np.sum(centered ** 2))
    if reference_energy <= 1:
        return result
    sums = np.r_[0, np.cumsum(captured)]
    squares = np.r_[0, np.cumsum(captured ** 2)]
    variance = squares[width:] - squares[:-width] - (sums[width:] - sums[:-width]) ** 2 / width
    variance = np.maximum(variance, 0)
    scores = np.divide(np.correlate(captured, centered, 'valid'), np.sqrt(variance * reference_energy),
                       out=np.zeros(len(captured) - width + 1), where=variance > 1)
    best = int(np.argmax(scores))
    peak = float(np.clip(scores[best], -1, 1))
    # 同じphonemeの周期内を別候補と数えず、20ms以上離れた再出現を確認する。
    scores[max(0, best - 320):best + 321] = 0
    competing = float(np.clip(np.max(scores), -1, 1))
    result.update(captured_start_sample=best, correlation=peak, competing_peak_correlation=competing,
                  accepted=peak >= MIN_CORRELATION and peak - competing >= MIN_PEAK_MARGIN)
    return result


def align_pcm(reference_pcm: bytes, captured_pcm: bytes, *, speech_start_sample: int,
              speech_end_sample: int) -> dict:
    """独立した前半・後半のanchorを照合し、sample範囲の診断だけを返す。"""
    reference, captured = _samples(reference_pcm), _samples(captured_pcm)
    if (type(speech_start_sample) is not int or type(speech_end_sample) is not int
            or not 0 <= speech_start_sample < speech_end_sample <= len(reference)):
        raise ValueError('reference speech boundaries invalid')
    duration = speech_end_sample - speech_start_sample
    result = {'method': 'two_anchor_normalized_pcm_correlation_v1', 'status': 'unverified',
              'reason': None, 'sample_rate_hz': SAMPLE_RATE, 'reference_sample_count': len(reference),
              'captured_sample_count': len(captured), 'anchors': [],
              'lag_difference_samples': None, 'alignment_tolerance_samples': ALIGNMENT_TOLERANCE_SAMPLES,
              'leading_loss_upper_ms': None, 'early_end_loss_upper_ms': None}
    if duration < 960:
        result['reason'] = 'reference_speech_too_short'
        return result
    midpoint = (speech_start_sample + speech_end_sample) // 2
    width = min(3200, duration // 3)
    anchors = [_match(captured, *_anchor(reference, low, high, width))
               for low, high in ((speech_start_sample, midpoint), (midpoint, speech_end_sample))]
    result['anchors'] = anchors
    if not all(anchor['accepted'] for anchor in anchors):
        result['reason'] = 'anchor_missing_or_ambiguous'
        return result
    offsets = [anchor['captured_start_sample'] - anchor['reference_start_sample'] for anchor in anchors]
    difference = max(offsets) - min(offsets)
    result['lag_difference_samples'] = difference
    if difference > MAX_LAG_DIFFERENCE_SAMPLES:
        result['reason'] = 'inconsistent_anchor_offsets'
        return result
    result.update(status='aligned',
                  leading_loss_upper_ms=max(0, -(speech_start_sample + min(offsets) - ALIGNMENT_TOLERANCE_SAMPLES)) / 16,
                  early_end_loss_upper_ms=max(0, speech_end_sample + max(offsets) + ALIGNMENT_TOLERANCE_SAMPLES - len(captured)) / 16)
    return result


def match_pcm_edges(reference_pcm: bytes, captured_pcm: bytes, *, speech_start_sample: int,
                    speech_end_sample: int) -> dict:
    """正解の冒頭・末尾を直接照合する。発話途中の同一offsetは要求・証明しない。"""
    reference, captured = _samples(reference_pcm), _samples(captured_pcm)
    if (type(speech_start_sample) is not int or type(speech_end_sample) is not int
            or not 0 <= speech_start_sample < speech_end_sample <= len(reference)):
        raise ValueError('reference speech boundaries invalid')
    duration = speech_end_sample - speech_start_sample
    result = {'method': 'direct_speech_edge_pcm_correlation_v1', 'status': 'unverified',
              'reason': None, 'sample_rate_hz': SAMPLE_RATE, 'captured_sample_count': len(captured),
              'anchors': [], 'leading_edge_captured': False, 'trailing_edge_captured': False,
              'interior_continuity_verified': False}
    if duration < 960:
        result['reason'] = 'reference_speech_too_short'
        return result
    width = min(3200, duration // 3)
    # 高energy部分へ寄せず、必ず正解の先頭・末尾を含む窓を使う。
    anchors = [_match(captured, start, reference[start:start + width])
               for start in (speech_start_sample, speech_end_sample - width)]
    result['anchors'] = anchors
    # 順序逆転や重複する別の音声へ対応したときは境界保持の証拠にしない。
    ordered = (all(row['accepted'] for row in anchors)
               and anchors[0]['captured_start_sample'] + width <= anchors[1]['captured_start_sample'])
    result['leading_edge_captured'] = bool(anchors[0]['accepted'])
    result['trailing_edge_captured'] = bool(anchors[1]['accepted'])
    result['status'] = 'matched' if ordered else 'unverified'
    result['reason'] = None if ordered else 'edge_missing_ambiguous_or_reordered'
    return result

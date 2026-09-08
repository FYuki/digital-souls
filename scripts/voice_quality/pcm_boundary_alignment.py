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


BOUNDED_EDGE_METHOD = 'bandlimited_100ms_speech_edge_witness_v3'
EDGE_FILTER_CUTOFF_HZ = 4000
EDGE_FILTER_TAPS = 129
EDGE_BLOCK_SAMPLES = 800
EDGE_SEARCH_RADIUS = 320
EDGE_INSET_SAMPLES = 400
EDGE_TOLERANCE_SAMPLES = 1600


def _edge_band(samples: np.ndarray) -> np.ndarray:
    # 符号化で変わる高域の雑音を照合へ使わない。PCM転送内容は変更しない。
    t = np.arange(-(EDGE_FILTER_TAPS // 2), EDGE_FILTER_TAPS // 2 + 1)
    kernel = (2 * EDGE_FILTER_CUTOFF_HZ / SAMPLE_RATE
              * np.sinc(2 * EDGE_FILTER_CUTOFF_HZ / SAMPLE_RATE * t) * np.hamming(EDGE_FILTER_TAPS))
    kernel /= kernel.sum()
    return np.convolve(samples, kernel, 'full')[EDGE_FILTER_TAPS // 2:EDGE_FILTER_TAPS // 2 + len(samples)]


def match_bounded_pcm_edges(reference_pcm: bytes, captured_pcm: bytes, *, speech_start_sample: int,
                            speech_end_sample: int) -> dict:
    """端部から25–75msの特徴波形を照合する。100ms超の欠けの検出が対象。"""
    reference, captured = _samples(reference_pcm), _samples(captured_pcm)
    if (type(speech_start_sample) is not int or type(speech_end_sample) is not int
            or not 0 <= speech_start_sample < speech_end_sample <= len(reference)):
        raise ValueError('reference speech boundaries invalid')
    result = {'method': BOUNDED_EDGE_METHOD, 'status': 'unverified', 'reason': 'edge_blocks_unverified',
              'sample_rate_hz': SAMPLE_RATE, 'captured_sample_count': len(captured),
              'filter_cutoff_hz': EDGE_FILTER_CUTOFF_HZ, 'filter_taps': EDGE_FILTER_TAPS,
              'block_samples': EDGE_BLOCK_SAMPLES, 'search_radius_samples': EDGE_SEARCH_RADIUS,
              'reference_edge_inset_samples': EDGE_INSET_SAMPLES, 'edge_tolerance_samples': EDGE_TOLERANCE_SAMPLES,
              'edges': [], 'interior_continuity_verified': False, 'full_band_quality_verified': False}
    duration = speech_end_sample - speech_start_sample
    if duration < 3200:
        result['reason'] = 'reference_speech_too_short'
        return result
    reference, captured = _edge_band(reference), _edge_band(captured)
    width, span = min(3200, duration // 3), min(9600, duration // 2)
    for leading in (True, False):
        low, high = ((speech_start_sample, speech_start_sample + span) if leading
                     else (speech_end_sample - span, speech_end_sample))
        position, pattern = _anchor(reference, low, high, width)
        seed = _match(captured, position, pattern)
        edge = {'side': 'leading' if leading else 'trailing', 'seed': seed, 'blocks': []}
        result['edges'].append(edge)
        if not seed['accepted']:
            continue
        lag = seed['captured_start_sample'] - position
        positions = ((speech_start_sample + EDGE_INSET_SAMPLES,) if leading
                     else (speech_end_sample - EDGE_INSET_SAMPLES - EDGE_BLOCK_SAMPLES,))
        for position in positions:
            low = max(0, position + lag - EDGE_SEARCH_RADIUS)
            high = min(len(captured), position + lag + EDGE_SEARCH_RADIUS + EDGE_BLOCK_SAMPLES)
            block = _match(captured[low:max(low, high)], position, reference[position:position + EDGE_BLOCK_SAMPLES])
            if block['captured_start_sample'] is not None:
                block['captured_start_sample'] += low
            # 局所窓の位置は一意なseedで制限済み。特徴窓はfilterの4msと探索幅20msを含めても端部99ms以内に収まる。
            block['accepted'] = block['correlation'] is not None and block['correlation'] >= MIN_CORRELATION
            edge['blocks'].append(block)
    if valid_bounded_pcm_edges(result, speech_start_sample, speech_end_sample, len(captured), require_status=False):
        result.update(status='matched', reason=None)
    return result


def valid_bounded_pcm_edges(edge: object, start: int, end: int, size: int, *, require_status: bool = True) -> bool:
    """保存した数値証拠でも、局所窓の位置・順序・相関条件を再検証する。"""
    import math
    def integer(value): return type(value) is int
    def score(value): return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
    if (not isinstance(edge, dict) or not integer(size) or size <= 0 or end - start < 3200
            or edge.get('method') != BOUNDED_EDGE_METHOD
            or edge.get('captured_sample_count') != size or edge.get('sample_rate_hz') != SAMPLE_RATE
            or edge.get('filter_cutoff_hz') != EDGE_FILTER_CUTOFF_HZ or edge.get('filter_taps') != EDGE_FILTER_TAPS
            or edge.get('block_samples') != EDGE_BLOCK_SAMPLES or edge.get('search_radius_samples') != EDGE_SEARCH_RADIUS
            or edge.get('reference_edge_inset_samples') != EDGE_INSET_SAMPLES
            or edge.get('edge_tolerance_samples') != EDGE_TOLERANCE_SAMPLES
            or EDGE_INSET_SAMPLES + EDGE_BLOCK_SAMPLES + EDGE_SEARCH_RADIUS + EDGE_FILTER_TAPS // 2 >= EDGE_TOLERANCE_SAMPLES
            or edge.get('interior_continuity_verified') is not False or edge.get('full_band_quality_verified') is not False
            or (require_status and (edge.get('status') != 'matched' or edge.get('reason') is not None))):
        return False
    edges = edge.get('edges')
    if not isinstance(edges, list) or len(edges) != 2:
        return False
    width, span = min(3200, (end - start) // 3), min(9600, (end - start) // 2)
    seeds, positions = [], []
    for item, leading in zip(edges, (True, False), strict=True):
        if not isinstance(item, dict) or item.get('side') != ('leading' if leading else 'trailing'):
            return False
        seed, blocks = item.get('seed'), item.get('blocks')
        if not isinstance(seed, dict) or not isinstance(blocks, list) or len(blocks) != 1:
            return False
        ref, cap = seed.get('reference_start_sample'), seed.get('captured_start_sample')
        low, high = (start, start + span) if leading else (end - span, end)
        ncc, competitor = seed.get('correlation'), seed.get('competing_peak_correlation')
        if (not integer(ref) or not low <= ref <= high - width or not integer(cap) or not 0 <= cap <= size - width
                or seed.get('sample_count') != width or seed.get('accepted') is not True
                or not score(ncc) or not score(competitor) or ncc < MIN_CORRELATION or ncc - competitor < MIN_PEAK_MARGIN):
            return False
        seeds.append(cap)
        local = []
        expected = (start + EDGE_INSET_SAMPLES,) if leading else (end - EDGE_INSET_SAMPLES - EDGE_BLOCK_SAMPLES,)
        for block, expected_start in zip(blocks, expected, strict=True):
            if not isinstance(block, dict): return False
            at, ncc = block.get('captured_start_sample'), block.get('correlation')
            if (block.get('reference_start_sample') != expected_start or block.get('sample_count') != EDGE_BLOCK_SAMPLES
                    or not integer(at) or not 0 <= at <= size - EDGE_BLOCK_SAMPLES
                    or abs(at - (expected_start + cap - ref)) > EDGE_SEARCH_RADIUS
                    or block.get('accepted') is not True or not score(ncc) or ncc < MIN_CORRELATION):
                return False
            local.append(at)
        positions.extend(local)
    return seeds[0] + width <= seeds[1] and positions[0] + EDGE_BLOCK_SAMPLES <= positions[1]

"""固定fixtureへ既知の欠けを注入し、端部照合の見逃しと正常系の欠測を数値で保存する。"""
from __future__ import annotations

import argparse
from collections import defaultdict, Counter
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import wave

import av
import numpy as np

from pcm_boundary_alignment import BOUNDED_EDGE_METHOD, match_bounded_pcm_edges
from whisper_pcm_observer import ROOT, WhisperPcmObserver


def opus_roundtrip(path: Path, bitrate: int) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (48000, 1, 2):
            raise ValueError('mono 48kHz PCM16 required')
        original = np.frombuffer(source.readframes(source.getnframes()), dtype='<i2')
    encoder = av.CodecContext.create('libopus', 'w')
    encoder.sample_rate, encoder.layout, encoder.format = 48000, 'mono', 's16'
    encoder.bit_rate, encoder.time_base = bitrate, Fraction(1, 48000)
    encoder.open()
    decoder = av.CodecContext.create('opus', 'r')
    decoded = []
    first_timestamp = None
    expected_timestamp = None

    def receive(packet):
        nonlocal first_timestamp, expected_timestamp
        for frame in decoder.decode(packet):
            if frame.sample_rate != 48000 or frame.pts is None or frame.time_base is None:
                raise ValueError('decoder timeline unavailable')
            timestamp = frame.pts * frame.time_base * 48000
            if timestamp.denominator != 1 or (expected_timestamp is not None and timestamp != expected_timestamp):
                raise ValueError('decoder timeline not contiguous')
            if first_timestamp is None:
                first_timestamp = int(timestamp)
            expected_timestamp = timestamp + frame.samples
            # decoderの平面float monoを元のPCM16単位へ戻す。
            samples = frame.to_ndarray()
            if (frame.format.name != 'fltp' or samples.shape[0] not in (1, 2)
                    or (samples.shape[0] == 2 and not np.array_equal(samples[0], samples[1]))):
                raise ValueError('unexpected decoder sample format')
            # headerなしraw packetは同一monoを左右へ複製するdecoderもある。差分0を確認して片側を使う。
            decoded.append(samples[0] * 32768)

    for index in range(0, len(original), 960):
        chunk = original[index:index + 960]
        data = np.zeros((1, 960), dtype=np.int16)
        data[0, :len(chunk)] = chunk
        frame = av.AudioFrame.from_ndarray(data, format='s16', layout='mono')
        frame.sample_rate, frame.pts, frame.time_base = 48000, index, Fraction(1, 48000)
        for packet in encoder.encode(frame): receive(packet)
    for packet in encoder.encode(None): receive(packet)
    if first_timestamp is None or first_timestamp > 0 or first_timestamp % 3:
        raise ValueError('codec source offset unavailable')
    return np.concatenate(decoded)[::3].astype('<i2'), -first_timestamp // 3


def calibrate(*, codec: str, bitrate: int, cohort: str | None = None) -> dict:
    fixture_root = ROOT / 'frontend/playwright/fixtures'
    manifest_bytes = (fixture_root / 'voice-quality-v2/manifest.json').read_bytes()
    normal = json.loads((fixture_root / 'speech.metadata.json').read_text())
    items = [{'cohort': 'normal', 'audio_sha256': normal['audio_sha256'], 'path': fixture_root / 'speech.wav'}]
    items.extend({**item, 'path': ROOT / 'frontend/test-results/vad-quality/fixtures-v2' / (item['id'] + '.wav')}
                 for item in json.loads(manifest_bytes)['trials'])
    if cohort is not None: items = [item for item in items if item['cohort'] == cohort]
    results = defaultdict(Counter)
    failures = []
    offsets = set()
    observer = WhisperPcmObserver(Path('/tmp/unused-local-calibration.jsonl'))
    try:
        for item in items:
            fixture = observer._fixture(item['audio_sha256'])
            reference = np.frombuffer(fixture['reference'], dtype='<i2')
            captured, offset = opus_roundtrip(item['path'], bitrate) if codec == 'opus' else (reference.copy(), 0)
            offsets.add(offset)
            a, b = fixture['start'] + offset, fixture['end'] + offset
            cut = 1616  # 16kHzの101ms。100msの品質基準を越える既知の欠け。
            cases = {
                'intact': captured,
                'leading_101ms_deleted': captured[a + cut:],
                'trailing_101ms_deleted': captured[:b - cut],
                'leading_101ms_silenced': np.r_[np.zeros(a + cut), captured[a + cut:]],
                'trailing_101ms_silenced': np.r_[captured[:b - cut], np.zeros(len(captured) - b + cut)],
                'duplicated': np.r_[captured, captured],
                'reordered': np.r_[captured[(a + b) // 2:], captured[:(a + b) // 2]],
            }
            for name, signal in cases.items():
                result = match_bounded_pcm_edges(fixture['reference'], signal.astype('<i2').tobytes(),
                    speech_start_sample=fixture['start'], speech_end_sample=fixture['end'])
                matched = result['status'] == 'matched'
                results[item['cohort']][name + '_total'] += 1
                results[item['cohort']][name + '_matched'] += matched
                if matched != (name == 'intact'):
                    failures.append({'fixture_sha256': item['audio_sha256'], 'cohort': item['cohort'],
                                     'case': name, 'status': result['status']})
    finally:
        observer.client.close()
    return {'scope': 'offline_endpoint_detector_calibration_only', 'method': BOUNDED_EDGE_METHOD,
            'codec': codec, 'bitrate': bitrate if codec == 'opus' else None,
            'codec_source_offsets_samples': sorted(offsets), 'pyav_version': av.__version__,
            'fixture_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
            'method_source_sha256': hashlib.sha256((ROOT / 'scripts/voice_quality/pcm_boundary_alignment.py').read_bytes()).hexdigest(),
            'fixture_count': len(items), 'counts': dict(results), 'failures': failures,
            'passed': not failures, 'actual_web_rtc_input_verified': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--codec', choices=('none', 'opus'), default='none')
    parser.add_argument('--bitrate', type=int, choices=(16000, 32000, 64000), default=32000)
    parser.add_argument('--cohort', choices=('normal', 'pause', 'backchannel', 'take_turn'))
    args = parser.parse_args()
    result = calibrate(codec=args.codec, bitrate=args.bitrate, cohort=args.cohort)
    with args.output.open('x') as output:
        output.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'fixtures': result['fixture_count'], 'failed_cases': len(result['failures']), 'passed': result['passed']}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

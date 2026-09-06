"""固定fixtureのVAD区間を実WhisperとCore判定へ渡す診断。Browser cancel受入ではない。"""
from __future__ import annotations

import argparse
import array
from collections import Counter
import json
from pathlib import Path
import sys
import time

from fixtures import DEFAULT_ROOT, ROOT, materialize_trial, read_pcm, sha256, validate

sys.path.insert(0, str(ROOT / 'backend'))
from app.conversation_core.adapters import _resample_pcm16  # noqa: E402
from app.conversation_core.turn_decision import classify_turn  # noqa: E402
from app.stt.remote_whisper_client import RemoteWhisperTranscriber  # noqa: E402


def run(root: Path, vad_path: Path, output: Path, whisper_url: str) -> None:
    validate(root, None)
    manifest_bytes = (root / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    vad_bytes = vad_path.read_bytes()
    vad = json.loads(vad_bytes)
    if vad['manifest_sha256'] != sha256(manifest_bytes):
        raise ValueError('VAD evidence belongs to a different fixture manifest')
    by_hash = {row['fixture_sha256']: row for row in vad['trials']}
    trials = []
    client = RemoteWhisperTranscriber(whisper_url)
    try:
        for fixture in manifest['trials']:
            if fixture['cohort'] not in ('backchannel', 'take_turn'):
                continue
            observed = by_hash[fixture['audio_sha256']]
            row = {'fixture_sha256': fixture['audio_sha256'], 'cohort': fixture['cohort'],
                   'outcome': 'missing', 'reason': None, 'decisions': [], 'stt_duration_ms': []}
            if not observed['segments']:
                row['reason'] = 'vad_no_finalized_utterance'
            else:
                wav, _ = materialize_trial(root, fixture)
                pcm = read_pcm(wav)
                decisions = []
                try:
                    for segment in observed['segments']:
                        start = round(segment['captured_start_ms'] * 48)
                        end = round(segment['captured_end_ms'] * 48)
                        if not 0 <= start < end <= len(pcm):
                            raise ValueError('VAD capture outside fixture')
                        samples = _resample_pcm16(list(pcm[start:end]), input_sample_rate=48_000, output_sample_rate=16_000)
                        audio = array.array('h', samples)
                        if sys.byteorder != 'little':
                            audio.byteswap()
                        before = time.monotonic_ns()
                        transcript = client.transcribe(audio.tobytes())
                        row['stt_duration_ms'].append((time.monotonic_ns() - before) / 1_000_000)
                        decisions.append(classify_turn(transcript))
                    row['decisions'] = decisions
                    row['outcome'] = 'observed'
                except Exception as error:
                    # URLや応答本文をartifactへ出さない。失敗を成功分母から消さない。
                    row['outcome'] = 'failure'
                    row['reason'] = type(error).__name__
            trials.append(row)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({'scope': 'offline_vad_real_whisper_core_classifier_diagnostic',
                                         'manifest_sha256': sha256(manifest_bytes), 'vad_evidence_sha256': sha256(vad_bytes),
                                         'expected_trials': 200, 'trials': trials}, indent=2) + '\n')
            if len(trials) % 25 == 0:
                print(json.dumps({'completed': len(trials), 'expected': 200}), flush=True)
    finally:
        client.close()
    summary = {}
    for cohort in ('backchannel', 'take_turn'):
        rows = [row for row in trials if row['cohort'] == cohort]
        summary[cohort] = {'denominator': len(rows), 'outcomes': dict(Counter(row['outcome'] for row in rows)),
                           'wrong_decision': sum(row['outcome'] == 'observed' and
                               (any(d == 'take_turn' for d in row['decisions']) if cohort == 'backchannel'
                                else not any(d == 'take_turn' for d in row['decisions'])) for row in rows),
                           'indeterminate': sum('indeterminate' in row['decisions'] for row in rows),
                           'missing_reasons': dict(Counter(row['reason'] for row in rows if row['outcome'] != 'observed'))}
    result = json.loads(output.read_text())
    result['summary'] = summary
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'summary': summary, 'output': str(output)}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--vad', type=Path, default=ROOT / 'frontend/test-results/vad-quality/labeled-v1.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'frontend/test-results/vad-quality/stt-turn-v1.json')
    parser.add_argument('--whisper-url', default='http://127.0.0.1:50022')
    args = parser.parse_args()
    run(args.root, args.vad, args.output, args.whisper_url)

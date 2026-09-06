from __future__ import annotations

import array
import importlib.util
import json
from pathlib import Path
import sys

import pytest

_PATH = Path(__file__).resolve().parents[3] / 'scripts/voice_quality/fixtures.py'
_SPEC = importlib.util.spec_from_file_location('voice_quality_fixtures', _PATH)
assert _SPEC is not None and _SPEC.loader is not None
fixtures = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fixtures
_SPEC.loader.exec_module(fixtures)


def fixture_manifest(root):
    cases = json.loads(fixtures.CASES_PATH.read_text())
    # 境界の数値を独立に求められる固定信号。実音声品質の成功を示すテストではない。
    pcm = array.array('h', [0] * 480 + [1000, -1000] * 240 + [0] * 480)
    data = fixtures.encode_pcm(pcm)
    sources = []
    for case in cases['cases']:
        name = f"{case['id']}.wav"
        (root / name).write_bytes(data)
        sources.append({'id': case['id'], 'cohort': case['cohort'], 'source_file': name,
                        'source_sha256': fixtures.sha256(data), 'speech_start_sample': 480,
                        'speech_end_sample': 960, 'speaker_id': 3, 'engine_version': 'test'})
    result = fixtures.build_manifest(root, cases, sources)
    (root / 'manifest.json').write_text(json.dumps(result))
    return result


def test_pcm_annotation_uses_fixed_source_boundary_and_exact_inserted_pause(tmp_path):
    manifest = fixture_manifest(tmp_path)
    trial = next(t for t in manifest['trials'] if t['cohort'] == 'pause' and t['pause_samples'] == 600 * 48)
    audio, annotation = fixtures.materialize_trial(tmp_path, trial)
    first, second = annotation['speech_intervals']
    assert second['start_sample'] - first['end_sample'] == 600 * 48
    assert first['start_sample'] == trial['leading_samples']
    pcm = fixtures.read_pcm(audio)
    assert all(sample == 0 for sample in pcm[first['end_sample']:second['start_sample']])
    assert len(pcm) == second['end_sample'] + 2 * 48_000
    fixtures.validate(tmp_path, None)


@pytest.mark.parametrize('change', ['label', 'boundary', 'gain', 'counts', 'traversal', 'audio', 'labels_hash'])
def test_validation_rejects_changed_labels_boundaries_recipes_or_sources(tmp_path, change):
    manifest = fixture_manifest(tmp_path)
    if change == 'label':
        manifest['sources'][0]['cohort'] = 'take_turn'
    elif change == 'boundary':
        manifest['sources'][0]['speech_start_sample'] = 0
    elif change == 'gain':
        manifest['trials'][0]['gain'] = 0.9
        _, annotation = fixtures.materialize_trial(tmp_path, manifest['trials'][0])
        manifest['trials'][0].update(annotation)
    elif change == 'counts':
        manifest['trials'].pop()
    elif change == 'traversal':
        manifest['sources'][0]['source_file'] = '../outside.wav'
    elif change == 'audio':
        (tmp_path / manifest['sources'][0]['source_file']).write_bytes(b'changed')
    elif change == 'labels_hash':
        manifest['cases_sha256'] = '0' * 64
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        fixtures.validate(tmp_path, None)


def test_silence_has_no_invented_speech_boundary():
    with pytest.raises(ValueError, match='no annotated speech'):
        fixtures.speech_bounds(array.array('h', [0] * 960))

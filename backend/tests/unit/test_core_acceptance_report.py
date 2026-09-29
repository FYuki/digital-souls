"""欠測・時計混在・過大推定を受入成功へ変換しないことを検証する。"""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts/voice_quality'))
import compare_core_acceptance as report


def delta(value, kind='stopped'):
    return dict(name=f'playback_estimate_delta_{kind}', session_id='s', response_id='r',
                value=value, outcome='success')


def test_delta_sign_and_missing_are_not_false_success():
    assert report.playback_deltas([])['status'] == 'INCONCLUSIVE'
    result = report.playback_deltas([delta(-1), delta(2, 'completed')])
    assert result['status'] == 'FAIL'
    assert result['overestimates'] == 1
    assert result['short_side']['p95'] == 2
    assert result['by_kind']['output_stop']['count'] == 0


@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), 1.5, None])
def test_invalid_delta_is_rejected(value):
    with pytest.raises(ValueError):
        report.playback_deltas([delta(value)])


def test_duplicate_observation_is_not_double_counted():
    with pytest.raises(ValueError):
        report.playback_deltas([delta(1), delta(1)])


def test_missing_run_and_incomplete_trial_are_inconclusive(tmp_path):
    missing = report.summarize(tmp_path)
    assert missing['status'] == 'NOT_RUN'
    assert report.compare(missing, missing)['status'] == 'INCONCLUSIVE'
    (tmp_path / 'trial-manifest.json').write_text(json.dumps(dict(
        measurement_revision='a' * 40, expected_measured=100, trials=[])))
    result = report.summarize(tmp_path)
    assert result['status'] == 'INCONCLUSIVE'
    assert result['first_text_after_stt_ms']['missing'] == 100
    assert result['resources']['cpu_percent']['status'] == 'missing'


@pytest.mark.parametrize('clock,expected', [('client_monotonic', None), ('server_monotonic', 25)])
def test_text_latency_never_subtracts_different_clocks(tmp_path, clock, expected):
    (tmp_path / 'trial-manifest.json').write_text(json.dumps(dict(
        measurement_revision='a' * 40, expected_measured=1,
        trials=[dict(phase='measured', sessionId='s', responseId='r')]
    )))
    directory = tmp_path / 'runtime-data/voice-metrics'
    directory.mkdir(parents=True)
    events = [dict(name=name, session_id='s', response_id='r', outcome='success',
                   timestamp=t, clock_domain=c, unit='nanosecond') for name,t,c in
              [('stt_completed', 1000000, 'server_monotonic'), ('first_text_delta', 26000000, clock)]]
    (directory / 'controlled-trace.jsonl').write_text('\n'.join(map(json.dumps, events)))
    assert report.summarize(tmp_path)['first_text_after_stt_ms']['p95'] == expected


@pytest.mark.parametrize('width,expected', [(20, 1200), (21, None)])
def test_playback_uses_causal_lower_bound_and_rejects_wide_clock(tmp_path, width, expected):
    trial = dict(phase='measured', sessionId='s', responseId='r', outcome='success',
                 transcript_matches=True, session_end_confirmed=True,
                 first_playback_method='audio_worklet_output_timestamp',
                 fixture_clock_method='audio_worklet_pcm_causal_bounds',
                 media_observation_method='response_track_stateful_opus_worklet_output',
                 fixture_speech_end_client_ms=100, startedAt=1300,
                 fixture_clock_bounds={'speechEnd': {'lowerMs': 100, 'upperMs': 100 + width}})
    (tmp_path / 'trial-manifest.json').write_text(json.dumps(dict(
        measurement_revision='a' * 40, expected_measured=1, trials=[trial])))
    directory = tmp_path / 'runtime-data/voice-metrics'
    directory.mkdir(parents=True)
    (directory / 'controlled-trace.jsonl').write_text(json.dumps(dict(
        name='first_playback', session_id='s', response_id='r', outcome='success',
        timestamp=1300, clock_domain='client_monotonic', unit='millisecond')))
    assert report.summarize(tmp_path)['fixture_end_to_playback_ms']['p95'] == expected


def test_numeric_comparison_never_claims_full_acceptance():
    run = dict(status='MEASURED', fixture_sha256='a', initial_state_hash='b', expected=100,
               first_text_after_stt_ms={'p95': 100}, fixture_end_to_playback_ms={'p95': 2100},
               resources={'cpu_percent': {'status': 'missing'}, 'memory_bytes': {'status': 'missing'}})
    result = report.compare(run, run)
    assert result['status'] == 'NUMERICAL_COMPARISON'
    assert result['acceptance'] == 'INCONCLUSIVE'
    assert result['after_playback_target_exceeded'] is True
    assert result['resource_change']['cpu_percent'] is None

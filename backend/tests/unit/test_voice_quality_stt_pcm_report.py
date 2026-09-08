from __future__ import annotations

import copy
from pathlib import Path
import sys
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/voice_quality'))
try:
    import report_stt_pcm as report
finally:
    sys.path.remove(str(ROOT / 'scripts/voice_quality'))


@pytest.fixture
def evidence():
    digest = 'b' * 64
    trials, observed, events = [], [], []
    for i in range(100):
        session, utterance = str(UUID(int=i + 1)), str(UUID(int=i + 1001))
        anchors = [{'reference_start_sample': start, 'sample_count': 3200,
                    'captured_start_sample': start + 3520, 'correlation': .99,
                    'competing_peak_correlation': .1, 'accepted': True} for start in [1600, 12800]]
        row = {'request_ordinal': i + 1, 'trial_ordinal': i + 1, 'phase': 'labeled',
               'fixture_sha256': digest, 'preparation_silence': False, 'input_sample_count': 32000,
               'started_ns': 100, 'completed_ns': 200, 'upstream_status': 200,
               'alignment': {'status': 'unverified', 'reason': 'inconsistent_anchor_offsets'},
               'edge_alignment': {'method': 'direct_speech_edge_pcm_correlation_v1', 'status': 'matched',
                    'reason': None, 'sample_rate_hz': 16000, 'captured_sample_count': 32000,
                    'anchors': anchors, 'leading_edge_captured': True, 'trailing_edge_captured': True,
                    'interior_continuity_verified': False}}
        observed.append(row)
        trials.append({'session_id': session, 'initial_utterance_id': str(UUID(int=i + 2001)),
            'fixture_sha256': digest, 'outcome': 'success', 'session_end_confirmed': True,
            'evidence': {'core_events': [{'type': 'utterance_finalized', 'utteranceId': utterance, 'sessionId': session}]},
            'pcm_input_observation': {'active_requests': 0, 'overflow': False, 'rows': [row]}})
        events.extend({'session_id': session, 'utterance_id': utterance, 'name': name, 'value': value}
                      for name, value in [('stt_input_prepared_sample_count', 32000),
                                          ('stt_input_suffix_preserved', 1), ('stt_capture_received_span_valid', 1)])
    return ({'cohort': 'pause', 'expected_measured': 100, 'measurement_revision': 'a' * 40, 'trials': trials},
            observed, events, {digest: (1600, 16000)})


def test_full_sample_correlation_and_edge_coverage_remain_narrow_in_scope(evidence):
    result = report.PcmReport(evidence_sha256={}, **report.summarize(*evidence))
    assert result.passed
    assert result.counts.edges_matched == result.counts.final_inputs_correlated == 100
    assert result.counts.nonuniform_global_alignment == 100
    assert not result.interior_continuity_verified
    assert not result.all_vad_acceptance_verified


@pytest.mark.parametrize('mutation,reason', [
    ('missing_trial', 'trial_not_recorded'), ('missing_final', 'final_utterance_unavailable'),
    ('preview_only', 'final_stt_capture_evidence_unavailable'), ('ambiguous_count', 'final_http_input_not_unique'),
    ('bad_edge', 'speech_edges_unverified'), ('wrong_boundary', 'speech_edges_unverified'),
    ('snapshot', 'observer_snapshot_mismatch'), ('active', 'input_observer_not_closed'),
    ('duplicate_session', 'independent_session_unavailable'), ('upstream', 'final_http_input_incomplete'),
])
def test_missing_or_invalid_evidence_stays_in_denominator(evidence, mutation, reason):
    m, rows, events, _ = evidence
    if mutation == 'missing_trial':
        m['trials'].pop()
    elif mutation == 'missing_final':
        m['trials'][0]['evidence']['core_events'] = []
    elif mutation == 'preview_only':
        events[0]['name'] = 'stt_preview_attempt_1_input_prepared_sample_count'
    elif mutation == 'ambiguous_count':
        duplicate = copy.deepcopy(rows[0]); duplicate['request_ordinal'] = 101
        rows.append(duplicate); m['trials'][0]['pcm_input_observation']['rows'].append(duplicate)
    elif mutation == 'bad_edge':
        rows[0]['edge_alignment']['anchors'][0]['correlation'] = .79
    elif mutation == 'wrong_boundary':
        rows[0]['edge_alignment']['anchors'][0]['reference_start_sample'] = 1601
    elif mutation == 'snapshot':
        m['trials'][0]['pcm_input_observation']['rows'] = []
    elif mutation == 'active':
        m['trials'][0]['pcm_input_observation']['active_requests'] = 1
    elif mutation == 'duplicate_session':
        m['trials'][1]['session_id'] = m['trials'][0]['session_id']
    else:
        rows[0]['upstream_status'] = 429
    result = report.summarize(*evidence)
    assert not result['passed']
    assert result['counts'].expected == 100 and result['counts'].missing == 1
    assert result['missing_reasons'] == {reason: 1}


def test_identical_request_ordinals_are_invalid_not_deduplicated(evidence):
    evidence[1].append(copy.deepcopy(evidence[1][0]))
    with pytest.raises(ValueError, match='request ordinal'):
        report.summarize(*evidence)


def test_unknown_revision_never_becomes_current_head(evidence):
    evidence[0].pop('measurement_revision')
    result = report.summarize(*evidence)
    assert result['measurement_revision'] is None
    assert not result['passed']


def test_warmup_is_not_counted_as_normal_measured_evidence(evidence):
    m, rows, events, catalog = evidence
    selected = m['trials'][:4]
    for i, trial in enumerate(selected):
        trial.update(sessionId=trial['session_id'], utteranceId=trial['evidence']['core_events'][0]['utteranceId'],
                     audio_sha256=trial['fixture_sha256'], phase='warmup' if i == 0 else 'measured')
    for row in rows:
        row['phase'] = 'initial'
    selected[0]['pcm_input_observation']['overflow'] = True
    m = {'expected_measured': 3, 'expected_warmup': 1, 'trials': selected}
    result = report.summarize(m, rows, events, catalog)
    assert result['counts'].expected == result['counts'].edges_matched == 3
    assert result['counts'].missing == 0
    assert not result['passed']

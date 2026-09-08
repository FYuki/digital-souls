from __future__ import annotations

import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator, ValidationError
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/voice_quality'))
try:
    import compare_normal_latency as report
finally:
    sys.path.remove(str(ROOT / 'scripts/voice_quality'))

CANDIDATE = ROOT / 'docs/artifacts/livekit-controlled-complete-playback-2026-09-08.json'
BASELINE = ROOT / 'docs/artifacts/websocket-baseline-v1.json'
VERIFICATION = ROOT / 'docs/artifacts/livekit-controlled-complete-playback-2026-09-08-verification.json'


@pytest.fixture
def artifacts():
    return (report.AggregateArtifact.model_validate_json(CANDIDATE.read_text()),
            report.AggregateArtifact.model_validate_json(BASELINE.read_text()))


def replace_metric(artifact, name, **updates):
    return artifact.model_copy(update={'metrics': [
        metric.model_copy(update=updates) if metric.name == name else metric for metric in artifact.metrics
    ]})


def test_observed_run_preserves_two_missing_vad_boundaries_and_strict_failure():
    result = report.build_report(CANDIDATE, BASELINE, VERIFICATION)
    assert not result.passed
    assert not result.strict_evaluation_passed
    assert result.coverage_errors == ['vad_leading_boundary:incomplete_measurement',
                                      'vad_trailing_boundary:incomplete_measurement']
    assert sum(row.status == 'passed' for row in result.relative_results.values()) == 8
    assert sum(row.status == 'not_comparable' for row in result.relative_results.values()) == 3
    assert all(row.passed for row in result.absolute_results.values())
    assert not result.absolute_results['ttfa'].median_guide_met
    assert 'local_playback_stop:incomplete_measurement' in result.strict_coverage_errors
    schema = json.loads((ROOT / 'docs/schemas/voice-quality-normal-latency-report-v1.schema.json').read_text())
    Draft202012Validator(schema).validate(result.model_dump(mode='json'))
    leaked = result.model_dump(mode='json')
    leaked['response'] = 'not permitted'
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(leaked)


def test_audited_interval_exemption_does_not_remove_absolute_utterance_limit(artifacts):
    candidate, baseline = artifacts
    candidate = replace_metric(candidate, 'utterance_finalized', p95=800.001)
    result = report.compare(candidate, baseline)
    assert result['relative_results']['utterance_finalized'].status == 'not_comparable'
    assert not result['absolute_results']['utterance_finalized'].passed
    assert not result['passed']


def test_unchanged_relative_limits_still_reject_slow_comparable_stage(artifacts):
    candidate, baseline = artifacts
    candidate = replace_metric(candidate, 'llm_start_latency', p95=51)
    result = report.compare(candidate, baseline)
    assert result['relative_results']['llm_start_latency'].status == 'failed'
    assert not result['passed']


@pytest.mark.parametrize('name', ['ttfa', 'response_decision', 'stt_processing'])
def test_partial_samples_are_never_hidden_by_p95_or_boundary_audit(artifacts, name):
    candidate, baseline = artifacts
    candidate = replace_metric(candidate, name, success_count=99, missing_count=1)
    result = report.compare(candidate, baseline)
    assert f'{name}:incomplete_measurement' in result['coverage_errors']
    assert not result['passed']


def test_unreviewed_boundary_change_and_duplicate_metrics_fail(artifacts):
    candidate, baseline = artifacts
    candidate = replace_metric(candidate, 'llm_start_latency', start_point='different_event')
    result = report.compare(candidate, baseline)
    assert result['relative_results']['llm_start_latency'].reason == 'measurement_boundary_mismatch'
    duplicate = candidate.model_copy(update={'metrics': [*candidate.metrics, candidate.metrics[0]]})
    assert 'candidate:duplicate_metric' in report.compare(duplicate, baseline)['coverage_errors']


def test_source_change_requires_new_audit(monkeypatch):
    monkeypatch.setattr(report, '_source_bytes', lambda *_: b'unreviewed source')
    with pytest.raises(ValueError, match='boundary audit required'):
        report.verify_sources('a' * 40)


@pytest.mark.parametrize('revision', ['HEAD', '-bad', '../private', 'a' * 39])
def test_revision_must_be_full_commit_id(revision):
    with pytest.raises(ValueError, match='full candidate revision required'):
        report.verify_sources(revision)


def test_candidate_revision_cannot_be_detached_from_verified_artifact(tmp_path):
    evidence = json.loads(VERIFICATION.read_text())
    evidence['artifact_sha256'] = '0' * 64
    wrong = tmp_path / 'verification.json'
    wrong.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match='verification hash mismatch'):
        report.build_report(CANDIDATE, BASELINE, wrong)


def test_frozen_baseline_cannot_be_rewritten(tmp_path):
    baseline = tmp_path / 'baseline.json'
    baseline.write_text(BASELINE.read_text() + '\n')
    with pytest.raises(ValueError, match='frozen baseline hash mismatch'):
        report.build_report(CANDIDATE, baseline, VERIFICATION)


def test_complete_vad_only_changes_normal_latency_scope(artifacts):
    candidate, baseline = artifacts
    references = {m.name: m for m in baseline.metrics}
    for name in ('vad_leading_boundary', 'vad_trailing_boundary'):
        # 合成テスト。実artifactの欠測を書き換えない。
        candidate = candidate.model_copy(update={'metrics': [
            references[name] if m.name == name else m for m in candidate.metrics]})
    result = report.compare(candidate, baseline)
    assert result['passed']
    assert not result['strict_evaluation_passed']
    assert 'turn_decision:incomplete_measurement' in result['strict_coverage_errors']


def test_historical_and_native_playback_revisions_keep_distinct_audited_hashes():
    historical = report.verify_sources('a03e8a69d318b257bb4bfcc17289df631db85058')
    current = report.verify_sources('1c060e99d6918f593c6278b512366532a649dfd2')
    assert historical['baseline'] == current['baseline']
    assert historical['candidate'] == report.AUDITED_CANDIDATE_VARIANTS[0]
    assert current['candidate'] == report.AUDITED_CANDIDATE_VARIANTS[1]
    assert historical['candidate'] != current['candidate']


def test_native_vad_boundary_audit_requires_complete_samples_and_exact_points(artifacts):
    candidate, baseline = artifacts
    reference = {metric.name: metric for metric in baseline.metrics}
    for name, (start, end) in report.LIVEKIT_VAD_POINTS.items():
        candidate = candidate.model_copy(update={'metrics': [
            reference[name].model_copy(update={'start_point':start, 'end_point':end}) if metric.name == name else metric
            for metric in candidate.metrics]})
    result = report.compare(candidate, baseline)
    assert result['passed']
    assert all(result['relative_results'][name].status == 'not_comparable' for name in report.LIVEKIT_VAD_POINTS)
    incomplete = replace_metric(candidate, 'vad_leading_boundary', success_count=99, missing_count=1)
    assert report.compare(incomplete, baseline)['relative_results']['vad_leading_boundary'].status == 'missing'
    wrong = replace_metric(candidate, 'vad_trailing_boundary', end_point='invented_capture_end')
    assert report.compare(wrong, baseline)['relative_results']['vad_trailing_boundary'].status == 'failed'


def test_native_vad_source_audit_cannot_be_skipped_for_modified_browser(monkeypatch):
    actual = report.verify_vad_sources('2d10020ca818fce228b8446d51c329bebccb92e5')
    assert actual == report.VAD_AUDITED_CANDIDATE_VARIANTS[0]
    monkeypatch.setattr(report, '_source_bytes', lambda *_: b'changed browser boundary')
    with pytest.raises(ValueError, match='boundary audit required'):
        report.verify_vad_sources('2d10020ca818fce228b8446d51c329bebccb92e5')

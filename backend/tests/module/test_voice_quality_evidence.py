from __future__ import annotations

import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator, ValidationError
import pytest

pytestmark = pytest.mark.cross_language

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


def test_historical_and_native_playback_revisions_keep_distinct_audited_hashes():
    historical = report.verify_sources('a03e8a69d318b257bb4bfcc17289df631db85058')
    current = report.verify_sources('1c060e99d6918f593c6278b512366532a649dfd2')
    assert historical['baseline'] == current['baseline']
    assert historical['candidate'] == report.AUDITED_CANDIDATE_VARIANTS[0]
    assert current['candidate'] == report.AUDITED_CANDIDATE_VARIANTS[1]
    assert historical['candidate'] != current['candidate']


def test_native_vad_source_audit_cannot_be_skipped_for_modified_browser(monkeypatch):
    actual = report.verify_vad_sources('2d10020ca818fce228b8446d51c329bebccb92e5')
    assert actual == report.VAD_AUDITED_CANDIDATE_VARIANTS[0]
    monkeypatch.setattr(report, '_source_bytes', lambda *_: b'changed browser boundary')
    with pytest.raises(ValueError, match='boundary audit required'):
        report.verify_vad_sources('2d10020ca818fce228b8446d51c329bebccb92e5')

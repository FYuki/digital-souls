from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "scripts/voice_quality/report_normal_diagnostic.py"
spec = importlib.util.spec_from_file_location("normal_diagnostic_tested", SOURCE)
assert spec and spec.loader
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def failed_manifest():
    return {
        "measurement_scope": "controlled", "measurement_revision": "a" * 40,
        "expected_warmup": 5, "expected_measured": 100,
        "fixture": {}, "initial_state_hash": "same-state",
        "trials": [{"phase": "warmup" if index < 5 else "measured", "outcome": "failure"}
                   for index in range(105)],
    }


def test_failed_trials_remain_in_every_metric_denominator():
    result = reporter.summarize(failed_manifest(), [])
    assert result["counts"]["recorded_measured"] == result["counts"]["failure"] == 100
    assert result["counts"]["validated_success"] == 0
    assert result["acceptance_passed"] is False
    for metric in result["metrics"]:
        assert metric["trial_count"] == metric["rate_denominator"] == 100
        if metric["name"] == "processing_failure":
            assert metric["failure_count"] == 100
        else:
            assert metric["missing_count"] == 100
            assert metric["p95"] is None


def test_partial_cohort_is_not_reported_as_one_hundred_trials():
    manifest = failed_manifest()
    manifest["trials"].pop()
    with pytest.raises(ValueError, match="105"):
        reporter.summarize(manifest, [])


def test_claimed_success_without_real_trace_is_missing():
    fixture = {"fixture_version": "fixed", "audio_sha256": "a" * 64}
    trial = {
        "outcome": "success", "transcript_matches": True, "session_end_confirmed": True,
        "initial_state_hash": "same-state", **fixture,
        "sessionId": "00000000-0000-4000-8000-000000000001",
        "utteranceId": "00000000-0000-4000-8000-000000000002",
        "responseId": "00000000-0000-4000-8000-000000000003",
        "conversationId": "00000000-0000-4000-8000-000000000004",
        "startedAt": 10.0,
    }
    metrics, reason = reporter.observations(trial, [], fixture, "same-state")
    assert reason == "trial_evidence_invalid"
    for metric in metrics.values():
        assert metric.status == "missing"
        assert metric.value is None

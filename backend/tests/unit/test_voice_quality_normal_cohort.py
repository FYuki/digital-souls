from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts/voice_quality"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("normal_cohort_tested", SCRIPTS / "run_normal_cohort.py")
assert spec and spec.loader
cohort = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cohort)


def test_plan_preserves_five_warmups_and_one_hundred_distinct_measurements():
    rows = cohort.plan("normal-test", 100)
    assert len(set(r for r, _ in rows)) == 105
    assert [phase for _, phase in rows] == ["warmup"] * 5 + ["measured"] * 100
    for identifier, count in [("../escape", 100), ("normal", True), ("normal", 0), ("normal", 101)]:
        with pytest.raises(ValueError):
            cohort.plan(identifier, count)


def test_unverified_environment_teardown_prevents_next_trial(tmp_path):
    path = tmp_path / "runtime-data/runtime/standalone"
    path.mkdir(parents=True)
    (path / "environment-run.json").write_text(json.dumps({
        "runtime": {"environmentId": "test", "dataRoot": str(tmp_path / "runtime-data")},
        "effectiveProfile": {"effectiveProfile": "integration-voice-pcm"},
        "teardown": {"status": "failed"},
    }))
    with pytest.raises(ValueError, match="teardown"):
        cohort.verify_stopped(tmp_path, "revision")


def test_failed_isolated_trials_stay_in_summary_and_do_not_create_acceptance_report(tmp_path, monkeypatch):
    runs = cohort.plan("normal-test", 1)
    monkeypatch.setattr(cohort, "run_root", lambda run_id: tmp_path / run_id)
    output = tmp_path / "summary"
    output.mkdir()
    for run_id, phase in runs:
        base = tmp_path / run_id
        trace = base / "runtime-data/voice-metrics"
        trace.mkdir(parents=True)
        (trace / "controlled-trace.jsonl").write_text("")
        (base / "trial-manifest.json").write_text(json.dumps({
            "initial_state_hash": "same-state", "trials": [{"phase": phase, "outcome": "failure"}],
        }))
    assert cohort.aggregate(runs, output, "revision", 1) == 1
    summary = json.loads((output / "summary.json").read_text())
    assert summary["recorded_trials"] == summary["failure"] == 6
    assert summary["success"] == 0
    assert summary["full_acceptance_passed"] is False
    assert not (output / "report.json").exists()

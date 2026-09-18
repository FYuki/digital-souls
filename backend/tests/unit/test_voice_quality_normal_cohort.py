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
        "effectiveProfile": {"effectiveProfile": "integration-voice"},
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


@pytest.mark.parametrize("profile", ["integration-voice", "integration-irodori"])
def test_standard_profile_allows_verified_teardown_but_pcm_profile_is_not_standard(tmp_path, monkeypatch, profile):
    from types import SimpleNamespace

    path = tmp_path / "runtime-data/runtime/standalone"
    path.mkdir(parents=True)
    report = {
        "runtime": {"environmentId": "test", "dataRoot": str((tmp_path / "runtime-data").resolve())},
        "effectiveProfile": {"effectiveProfile": profile},
        "teardown": {"status": "completed"},
        "services": {name: {"owned": True, "containerIdentity": {"containerId": letter * 64}}
                     for name, letter in [("backend", "a"), ("frontend", "b")]},
    }
    (path / "environment-run.json").write_text(json.dumps(report))
    (tmp_path / "native-sdk.json").write_text(json.dumps({"status": "verified"}))
    (tmp_path / "trial-manifest.json").write_text(json.dumps({
        "measurement_revision": "revision", "measurement_scope": "isolated_normal_trial",
    }))
    monkeypatch.setattr(cohort.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(returncode=1, stderr="No such object"))
    assert cohort.verify_stopped(tmp_path, "revision", profile)["measurement_scope"] == "isolated_normal_trial"
    report["effectiveProfile"]["effectiveProfile"] = "integration-voice-pcm"
    (path / "environment-run.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="teardown"):
        cohort.verify_stopped(tmp_path, "revision", profile)

def test_unknown_profile_is_not_accepted_as_an_independent_normal_trial(tmp_path):
    with pytest.raises(ValueError, match="profile"):
        cohort.verify_stopped(tmp_path, "revision", "integration-irodori-fault")


def test_preparation_summary_keeps_missing_failed_and_invalid_attempts():
    def observation(**change):
        return {"preparation_observation": {
            "method": "browser_click_to_microphone_standby_dom_v1",
            "clock_domain": "browser_performance", "attempt_count": 1,
            "started_client_ms": 100, "ready_client_ms": 120100, "duration_ms": 120000,
            **change,
        }}
    summary = cohort.preparation_summary([
        observation(), observation(ready_client_ms=None, duration_ms=None), {},
        observation(attempt_count=2), observation(duration_ms=-1),
        observation(ready_client_ms=float("nan")), observation(duration_ms=10),
        observation(clock_domain="server_monotonic"),
    ])
    assert summary["denominator"] == 8
    assert (summary["ready"], summary["not_ready"], summary["missing"], summary["invalid"]) == (1, 1, 1, 5)
    assert summary["coverage"] == 1 / 8
    assert summary["duration_ms_p95"] == 120000
    empty = cohort.preparation_summary([])
    assert empty["duration_ms_p95"] is None
    assert empty["coverage"] == 0

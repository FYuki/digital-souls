import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "cohort_report_validation",
    ROOT / "scripts/voice_quality/cohort_report_validation.py",
)
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)
METRIC_PATH = ROOT / "docs/schemas/voice-quality-artifact-v1.schema.json"
METRIC_SCHEMA = json.loads(METRIC_PATH.read_text())
COHORT_SCHEMA = json.loads(validation.COHORT_SCHEMA_PATH.read_text())


def artifact(name):
    return json.loads((ROOT / "docs/artifacts" / name).read_text())


def take_report():
    report = artifact("livekit-take-turn-pilot-2026-09-07-one-second.json")
    validation.stamp_and_validate_report(report, METRIC_PATH)
    return report


@pytest.mark.parametrize(
    "name",
    [
        "livekit-take-turn-pilot-2026-09-07-one-second.json",
        "livekit-take-turn-pilot-2026-09-07-idle-clock.json",
        "livekit-take-turn-100-2026-09-07-short-speech.json",
        "livekit-backchannel-100-2026-09-07-ambiguous-reaction.json",
    ],
)
def test_success_missing_and_indeterminate_reports_preserve_their_evaluation(name):
    report = artifact(name)
    original = copy.deepcopy(report)
    validation.stamp_and_validate_report(report, METRIC_PATH)
    assert all(report[key] == value for key, value in original.items())
    assert report["cohort_schema_version"] == "1.0"
    assert len(report["cohort_schema_sha256"]) == 64


@pytest.mark.parametrize(
    "damage",
    [
        "wrong_status",
        "wrong_boundary",
        "missing_evaluation",
        "unknown_metric",
        "wrong_count_type",
        "incomplete_trials",
        "wrong_denominator",
        "missing_reason",
        "nonfinite_quantile",
        "reversed_quantiles",
        "wrong_rate",
        "false_latency_result",
        "pilot_reported_as_accepted",
        "extra_private_field",
    ],
)
def test_rejects_incomplete_or_misleading_aggregate_without_exposing_values(damage):
    report = take_report()
    metric = report["metrics"][0]
    if damage == "wrong_status":
        metric["status"] = "missing"
    elif damage == "wrong_boundary":
        metric["start_point"] = "private-test-value-never-in-error"
    elif damage == "missing_evaluation":
        report.pop("evaluation")
    elif damage == "unknown_metric":
        metric["name"] = "unknown"
    elif damage == "wrong_count_type":
        report["counts"]["success"] = "8"
    elif damage == "incomplete_trials":
        report["counts"]["recorded"] -= 1
    elif damage == "wrong_denominator":
        metric["rate_denominator"] -= 1
    elif damage == "missing_reason":
        metric["success_count"] -= 1
        metric["missing_count"] += 1
    elif damage == "nonfinite_quantile":
        metric["p95"] = float("nan")
    elif damage == "reversed_quantiles":
        metric["p50"] = metric["p95"] + 1
    elif damage == "wrong_rate":
        metric["failure_rate_basis_points"] = 1
    elif damage == "false_latency_result":
        report["evaluation"]["latency_passed"]["local_playback_stop"] = False
    elif damage == "pilot_reported_as_accepted":
        report["evaluation"]["missed_rate_passed"] = True
    elif damage == "extra_private_field":
        report["prompt"] = "private-test-value-never-in-error"
    with pytest.raises(ValueError) as captured:
        validation.validate_report(report, METRIC_SCHEMA, COHORT_SCHEMA)
    assert "private-test-value" not in str(captured.value)


@pytest.mark.parametrize(
    "damage", ["classification_count", "false_pass", "failure_stages"]
)
def test_backchannel_does_not_hide_indeterminate_or_failed_trials(damage):
    report = artifact("livekit-backchannel-100-2026-09-07-ambiguous-reaction.json")
    validation.stamp_and_validate_report(report, METRIC_PATH)
    if damage == "classification_count":
        report["counts"]["unverified_final_decision"] -= 1
    elif damage == "false_pass":
        report["evaluation"]["false_cancel_rate_passed"] = False
    else:
        report["failure_stages"] = {}
    with pytest.raises(ValueError):
        validation.validate_report(report, METRIC_SCHEMA, COHORT_SCHEMA)

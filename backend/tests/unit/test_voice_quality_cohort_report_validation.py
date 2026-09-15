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


def fixture_report():
    report = take_report()
    report.update(
        latency_origin="scheduled_fixture_speech_start", input_authority="backend"
    )
    report["fixture_latency_bounds"] = {}
    for metric in report["metrics"]:
        if metric["name"] == "cancel_after_decision":
            continue
        metric["start_point"] = "fixture_speech_start_lower_bound"
        report["fixture_latency_bounds"][metric["name"]] = {
            "measured_count": metric["success_count"],
            "missing_count": metric["missing_count"],
            **{
                f"p{q}_{side}_ms": (
                    None
                    if metric[f"p{q}"] is None
                    else max(0, metric[f"p{q}"] - offset)
                )
                for q in (50, 95)
                for side, offset in (("lower", 2), ("upper", 0))
            },
        }
    return report


@pytest.mark.parametrize("authority", ["frontend", "backend"])
def test_fixture_reports_keep_conservative_latency_and_existing_acceptance(authority):
    report = fixture_report()
    report["input_authority"] = authority
    evaluation = copy.deepcopy(report["evaluation"])
    validation.stamp_and_validate_report(report, METRIC_PATH)
    assert report["evaluation"] == evaluation


@pytest.mark.parametrize(
    "damage",
    [
        "missing_origin",
        "missing_authority",
        "missing_bounds",
        "wrong_authority",
        "legacy_boundary",
        "legacy_with_fixture_boundary",
        "wrong_count",
        "optimistic_upper",
        "reversed_interval",
        "nonfinite_lower",
        "null_lower",
    ],
)
def test_fixture_reports_reject_misleading_origins_bounds_or_denominators(damage):
    report = fixture_report()
    metric = report["metrics"][0]
    bounds = report["fixture_latency_bounds"][metric["name"]]
    if damage.startswith("missing_"):
        report.pop(
            {
                "missing_origin": "latency_origin",
                "missing_authority": "input_authority",
                "missing_bounds": "fixture_latency_bounds",
            }[damage]
        )
    elif damage == "wrong_authority":
        report["input_authority"] = "unknown"
    elif damage == "legacy_boundary":
        metric["start_point"] = "speech_started_client"
    elif damage == "legacy_with_fixture_boundary":
        for key in ("latency_origin", "input_authority", "fixture_latency_bounds"):
            report.pop(key)
    elif damage == "wrong_count":
        bounds["measured_count"] -= 1
        bounds["missing_count"] += 1
    elif damage == "optimistic_upper":
        bounds["p95_upper_ms"] += 50
    elif damage == "reversed_interval":
        bounds["p50_lower_ms"] = bounds["p50_upper_ms"] + 1
    elif damage == "nonfinite_lower":
        bounds["p50_lower_ms"] = float("inf")
    elif damage == "null_lower":
        bounds["p50_lower_ms"] = None
    with pytest.raises(ValueError):
        validation.validate_report(report, METRIC_SCHEMA, COHORT_SCHEMA)

"""ラベル付き集計全体を、schema・全分母・欠測理由・合否の整合性で検証する。"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from app.voice_baseline import _assert_anonymous
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

COHORT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs/schemas/voice-quality-cohort-report-v1.schema.json"
)
METRIC_SCHEMA_URI = (
    "https://digital-souls.local/schemas/voice-quality-artifact-v1.schema.json"
)
LATENCY_LIMITS = {
    "local_playback_stop": 3000,
    "turn_decision": 3000,
    "cancel_after_decision": 200,
    "barge_in_cancel_total": 3500,
}


def _require(condition: bool, reason: str) -> None:
    if not condition:
        # 不正なreportに本文や秘密値が紛れ込んでも例外へ転記しない。
        raise ValueError(reason)


def validate_report(
    report: dict[str, Any], metric_schema: dict[str, Any], cohort_schema: dict[str, Any]
) -> None:
    registry = Registry().with_resource(
        METRIC_SCHEMA_URI, Resource.from_contents(metric_schema)
    )
    Draft202012Validator.check_schema(cohort_schema)
    validator = Draft202012Validator(cohort_schema, registry=registry)
    # ValidationErrorのinstanceをCLI出力へ持ち出さない。
    _require(validator.is_valid(report), "cohort report schema mismatch")
    _assert_anonymous(report)
    counts = report["counts"]
    expected = counts["expected"]
    _require(
        counts["recorded"] == expected == counts["success"] + counts["failure"],
        "cohort trial denominator mismatch",
    )
    _require(
        all(value <= expected for value in counts.values()),
        "cohort count exceeds trial denominator",
    )
    _require(
        sum(report["failure_stages"].values()) == counts["failure"],
        "cohort failure stages mismatch",
    )
    for metric in report["metrics"]:
        _require(metric["trial_count"] == expected, "metric trial denominator mismatch")
        total = sum(
            metric[key]
            for key in (
                "success_count",
                "failure_count",
                "missing_count",
                "not_applicable_count",
                "excluded_count",
            )
        )
        _require(total == expected, "metric outcomes do not cover all trials")
        status = next(
            label
            for key, label in (
                ("failure_count", "failed"),
                ("missing_count", "missing"),
                ("success_count", "measured"),
                ("not_applicable_count", "not_applicable"),
                ("excluded_count", "excluded"),
            )
            if metric[key]
        )
        _require(metric["status"] == status, "metric status contradicts observations")
        denominator = (
            metric["success_count"] + metric["failure_count"] + metric["missing_count"]
        )
        _require(
            metric["rate_denominator"] == denominator,
            "metric rate denominator mismatch",
        )
        expected_rate = (
            round(metric["failure_count"] * 10000 / denominator)
            if denominator
            else None
        )
        _require(
            metric["failure_rate_basis_points"] == expected_rate,
            "metric failure rate mismatch",
        )
        _require(
            sum(metric.get("missing_outcomes", {}).values()) == metric["missing_count"],
            "metric missing reasons mismatch",
        )
        _require(
            sum(metric["excluded_outcomes"].values()) == metric["excluded_count"],
            "metric excluded reasons mismatch",
        )
        quantiles = (metric["p50"], metric["p95"])
        if metric["success_count"]:
            _require(
                all(
                    type(value) in (int, float) and math.isfinite(value)
                    for value in quantiles
                ),
                "metric quantiles unavailable",
            )
            _require(
                0 <= quantiles[0] <= quantiles[1], "metric quantile order mismatch"
            )
        else:
            _require(quantiles == (None, None), "metric quantiles without observations")
    evaluation = report["evaluation"]
    if report["cohort"] == "take_turn":
        injected, cancelled = counts["verified_injection"], counts["verified_cancel"]
        _require(
            cancelled <= min(injected, counts["take_turn_decision"]),
            "cancel count exceeds verified inputs",
        )
        missed = injected - cancelled
        _require(
            report["missed_interruptions"]
            == {
                "count": missed,
                "denominator": injected,
                "unverified_injection_count": expected - injected,
                "rate_basis_points": round(missed * 10000 / injected)
                if injected
                else None,
            },
            "missed interruption denominator mismatch",
        )
        coverage = (
            injected == expected
            and counts["independent_sessions"] == expected
            and counts["session_end_confirmed"] == expected
        )
        _require(
            evaluation["coverage_complete"] == coverage,
            "take-turn coverage evaluation mismatch",
        )
        _require(
            evaluation["missed_rate_passed"]
            == (expected >= 100 and coverage and missed * 100 <= expected),
            "missed interruption evaluation mismatch",
        )
        latency = {
            metric["name"]: metric["missing_count"] == 0
            and metric["p95"] is not None
            and metric["p95"] <= LATENCY_LIMITS[metric["name"]]
            for metric in report["metrics"]
        }
        _require(evaluation["latency_passed"] == latency, "latency evaluation mismatch")
    else:
        _require(
            sum(
                counts[key]
                for key in (
                    "backchannel_decision",
                    "take_turn_decision",
                    "indeterminate_decision",
                    "unverified_final_decision",
                )
            )
            == expected,
            "classification outcomes do not cover all trials",
        )
        metric = report["metrics"][0]
        coverage = (
            metric["missing_count"] == 0
            and counts["independent_sessions"] == expected
            and counts["session_end_confirmed"] == expected
        )
        _require(
            evaluation["coverage_complete"] == coverage,
            "backchannel coverage evaluation mismatch",
        )
        _require(
            evaluation["false_cancel_rate_passed"]
            == (
                expected >= 100
                and coverage
                and metric["failure_count"] * 100 <= expected * 2
            ),
            "backchannel cancellation evaluation mismatch",
        )


def stamp_and_validate_report(report: dict[str, Any], metric_schema_path: Path) -> None:
    schema_bytes = COHORT_SCHEMA_PATH.read_bytes()
    report["cohort_schema_version"] = "1.0"
    report["cohort_schema_sha256"] = hashlib.sha256(schema_bytes).hexdigest()
    report["cohort_validator_sha256"] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    validate_report(
        report, json.loads(metric_schema_path.read_bytes()), json.loads(schema_bytes)
    )

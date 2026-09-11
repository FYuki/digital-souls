"""ラベル付きtake-turnの全分母と相関済み遅延を匿名化する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.voice_baseline import _assert_anonymous, _load_trace
from app.voice_metrics import (
    MetricObservation,
    TraceEvent,
    aggregate_metric,
    duration_ms,
)
from jsonschema import Draft202012Validator

POINTS = {
    "local_playback_stop": (
        "speech_started_client",
        "local_playback_stopped",
        "client_monotonic",
        "millisecond",
        3000,
    ),
    "turn_decision": (
        "speech_started_client",
        "turn_decision_client",
        "client_monotonic",
        "millisecond",
        3000,
    ),
    "cancel_after_decision": (
        "take_turn_decision",
        "server_cancelled",
        "server_monotonic",
        "nanosecond",
        200,
    ),
    "barge_in_cancel_total": (
        "speech_started_client",
        "server_cancelled_client",
        "client_monotonic",
        "millisecond",
        3500,
    ),
}
STAGES = {
    "initial_response",
    "playback_overlap",
    "fixture_and_decision",
    "cancel_or_continuity",
}


def number(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid observation timestamp")
    return float(value)


def bounds_valid(trial: dict[str, Any], fixture: dict[str, Any]) -> bool:
    bounds = trial.get(
        "fixture_clock_bounds", trial.get("evidence", {}).get("fixture_clock_bounds")
    )
    if not isinstance(bounds, dict):
        return False
    samples = [
        0,
        fixture["speech_intervals"][0]["start_sample"],
        fixture["speech_intervals"][-1]["end_sample"],
    ]
    lows, highs = [], []
    for key, sample in zip(
        ("sourceStart", "speechStart", "speechEnd"), samples, strict=True
    ):
        row = bounds.get(key)
        if (
            not isinstance(row, dict)
            or type(row.get("sourceSample")) is not int
            or row["sourceSample"] != sample
        ):
            return False
        try:
            low, high = number(row.get("lowerMs")), number(row.get("upperMs"))
        except ValueError:
            return False
        if not 0 <= high - low <= 20:
            return False
        lows.append(low)
        highs.append(high)
    return lows == sorted(lows) and highs == sorted(highs)


def latency(
    events: Sequence[TraceEvent], definition: tuple[str, str, str, str, int]
) -> MetricObservation:
    start, end, domain, unit, _ = definition
    points = [[event for event in events if event.name == key] for key in (start, end)]
    if any(len(items) > 1 for items in points):
        return MetricObservation.missing("trace_point_duplicated")
    if any(not items for items in points):
        return MetricObservation.missing("trace_point_unavailable")
    first, last = points[0][0], points[1][0]
    if any(event.outcome != "success" for event in (first, last)):
        return MetricObservation.missing("trace_point_not_successful")
    if any(
        event.clock_domain != domain or event.unit != unit for event in (first, last)
    ):
        return MetricObservation.missing("trace_clock_mismatch")
    try:
        number(first.timestamp)
        number(last.timestamp)
        value = duration_ms(first, last)
    except ValueError:
        return MetricObservation.missing("trace_timestamp_invalid")
    return MetricObservation.measured(value)


def summarize(
    manifest: dict[str, Any], events: Sequence[TraceEvent], fixtures: dict[str, Any]
) -> dict[str, Any]:
    if (
        manifest.get("measurement_scope") != "labeled_livekit_interruption_diagnostic"
        or manifest.get("cohort") != "take_turn"
    ):
        raise ValueError("take-turn diagnostic manifest required")
    count = manifest.get("expected_measured")
    trials = manifest.get("trials")
    if (
        type(count) is not int
        or not 1 <= count <= 100
        or not isinstance(trials, list)
        or len(trials) != count
    ):
        raise ValueError("all expected trials must be recorded before reporting")
    selected = [
        trial for trial in fixtures["trials"] if trial["cohort"] == "take_turn"
    ][:count]
    if len(selected) != count or [t["fixture_sha256"] for t in trials] != [
        t["audio_sha256"] for t in selected
    ]:
        raise ValueError("labeled fixture coverage or order mismatch")
    identities = [t.get("session_id") for t in trials if t.get("session_id")]
    if len(set(identities)) != len(identities):
        raise ValueError("sessions must be independent")
    if len({event.character_id for event in events}) > 1 or any(
        event.measurement_kind != "controlled_baseline" for event in events
    ):
        raise ValueError("trace mixes characters or measurement kinds")
    observations: dict[str, list[MetricObservation]] = {name: [] for name in POINTS}
    failure_stages: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    for trial, fixture in zip(trials, selected, strict=True):
        if trial.get("outcome") not in ("success", "failure"):
            raise ValueError("invalid trial outcome")
        if trial["outcome"] == "failure":
            stage = trial.get("failure_stage")
            if stage is None and trial.get("cleanup_failed") is True:
                stage = "cleanup"
            if stage not in STAGES | {"cleanup"}:
                raise ValueError("invalid failure stage")
            failure_stages[stage] += 1
        outcomes[trial["outcome"]] += 1
        outcomes["session_end_confirmed"] += trial.get("session_end_confirmed") is True
        evidence = trial.get("evidence", {})
        injected = trial.get("injection_playback", {}).get(
            "active"
        ) is True and bounds_valid(trial, fixture)
        outcomes["verified_injection"] += injected
        decision = trial.get("decision")
        matched_decision = (
            isinstance(decision, dict)
            and decision.get("final") is True
            and decision.get("responseId") == trial.get("old_response_id")
            and decision.get("sessionId") == trial.get("session_id")
        )
        take_turn = matched_decision and decision.get("decision") == "take_turn"
        outcomes["take_turn_decision"] += bool(take_turn)
        pair = (
            (
                trial.get("session_id"),
                decision.get("utteranceId"),
                trial.get("old_response_id"),
            )
            if matched_decision
            else None
        )
        correlated = [
            e
            for e in events
            if pair is not None
            and (e.session_id, e.utterance_id, e.response_id) == pair
        ]
        stops = [
            stop
            for stop in evidence.get("interruptions", [])
            if stop.get("responseId") == trial.get("old_response_id")
        ]
        cancelled = take_turn and any(
            e.get("type") == "response_cancelled"
            and e.get("responseId") == trial.get("old_response_id")
            and e.get("sessionId") == trial.get("session_id")
            for e in evidence.get("core_events", [])
        )
        verified_stop = False
        verified_ack = False
        if len(stops) == 1:
            points = {
                name: [e for e in correlated if e.name == name]
                for name in (
                    "speech_started_client",
                    "local_playback_stopped",
                    "server_cancelled_client",
                )
            }

            def client_point(
                name: str, observation_points: dict[str, list[TraceEvent]] = points,
            ) -> TraceEvent | None:
                rows = observation_points[name]
                if len(rows) != 1:
                    return None
                event = rows[0]
                if (
                    event.clock_domain != "client_monotonic"
                    or event.unit != "millisecond"
                    or event.outcome != "success"
                ):
                    return None
                return event

            first, stopped, acknowledged = (client_point(name) for name in points)
            stop = stops[0]
            if first is not None and stopped is not None:
                try:
                    verified_stop = (
                        number(stop.get("speechStartedAtMs")) == number(first.timestamp)
                        and abs(
                            number(stop.get("localPlaybackStoppedAtMs"))
                            - number(stopped.timestamp)
                        )
                        <= 2
                    )
                except ValueError:
                    pass
            if acknowledged is not None:
                try:
                    verified_ack = (
                        abs(
                            number(stop.get("cancelConfirmedAtMs"))
                            - number(acknowledged.timestamp)
                        )
                        <= 2
                    )
                except ValueError:
                    pass
        verified_cancel = bool(
            injected and cancelled and verified_stop and verified_ack
        )
        outcomes["verified_cancel"] += verified_cancel
        for name, definition in POINTS.items():
            reason = (
                "fixture_injection_unverified"
                if not injected
                else "final_decision_unavailable"
                if not matched_decision
                else "take_turn_not_selected"
                if not take_turn
                else None
            )
            observation = (
                MetricObservation.missing(reason)
                if reason
                else latency(correlated, definition)
            )
            if observation.status == "measured":
                if name == "local_playback_stop" and not verified_stop:
                    observation = MetricObservation.missing(
                        "browser_stop_correlation_unavailable"
                    )
                elif name == "barge_in_cancel_total" and not verified_ack:
                    observation = MetricObservation.missing(
                        "browser_cancel_confirmation_unavailable"
                    )
                elif name == "cancel_after_decision" and not cancelled:
                    observation = MetricObservation.missing(
                        "server_cancel_not_confirmed"
                    )
            observations[name].append(observation)
    metrics = [
        aggregate_metric(
            name, observations[name], start_point=definition[0], end_point=definition[1]
        ).model_dump(mode="json")
        for name, definition in POINTS.items()
    ]
    missed = outcomes["verified_injection"] - outcomes["verified_cancel"]
    full_coverage = (
        outcomes["verified_injection"] == count
        and len(identities) == count
        and outcomes["session_end_confirmed"] == count
    )
    result = {
        "scope": "labeled_livekit_take_turn_cohort_report",
        "cohort": "take_turn",
        "observer_timestamp_tolerance_ms": 2,
        "quantile_method": "hyndman_fan_type_7",
        "counts": {
            "expected": count,
            "recorded": len(trials),
            **{
                key: outcomes[key]
                for key in (
                    "success",
                    "failure",
                    "verified_injection",
                    "take_turn_decision",
                    "verified_cancel",
                    "session_end_confirmed",
                )
            },
            "independent_sessions": len(identities),
        },
        "failure_stages": dict(failure_stages),
        "missed_interruptions": {
            "count": missed,
            "denominator": outcomes["verified_injection"],
            "unverified_injection_count": count - outcomes["verified_injection"],
            "rate_basis_points": round(missed * 10000 / outcomes["verified_injection"])
            if outcomes["verified_injection"]
            else None,
        },
        "metrics": metrics,
        "evaluation": {
            "minimum_trials": 100,
            "coverage_complete": full_coverage,
            "missed_rate_passed": count >= 100
            and full_coverage
            and missed * 100 <= count,
            "latency_passed": {
                m["name"]: m["missing_count"] == 0
                and m["p95"] is not None
                and m["p95"] <= POINTS[m["name"]][4]
                for m in metrics
            },
        },
    }
    _assert_anonymous(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "trace", "fixtures", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "docs/schemas/voice-quality-artifact-v1.schema.json",
    )
    args = parser.parse_args()
    raw = args.fixtures.read_bytes()
    manifest = json.loads(args.manifest.read_text())
    if hashlib.sha256(raw).hexdigest() != manifest.get("labeled_manifest_sha256"):
        raise ValueError("labeled manifest hash mismatch")
    report = summarize(manifest, _load_trace(args.trace), json.loads(raw))
    schema = json.loads(args.schema.read_text())
    metric_schema = {
        "$schema": schema["$schema"],
        "$defs": schema["$defs"],
        "$ref": "#/$defs/metric",
    }
    Draft202012Validator.check_schema(metric_schema)
    validator = Draft202012Validator(metric_schema)
    for metric in report["metrics"]:
        validator.validate(metric)
    report["metric_schema_sha256"] = hashlib.sha256(
        args.schema.read_bytes()
    ).hexdigest()
    report["labeled_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    report["raw_manifest_sha256"] = hashlib.sha256(
        args.manifest.read_bytes()
    ).hexdigest()
    report["raw_trace_sha256"] = hashlib.sha256(args.trace.read_bytes()).hexdigest()
    report["reporter_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    from cohort_report_validation import stamp_and_validate_report

    stamp_and_validate_report(report, args.schema)
    with args.output.open("x") as output:
        output.write(
            json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        )
    print(json.dumps({"counts": report["counts"], "evaluation": report["evaluation"]}))


if __name__ == "__main__":
    main()

"""ラベル付きtake-turnの全分母と相関済み遅延を匿名化する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
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
    type7_quantile,
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
    "microphone_activation",
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


def client_point(
    events: Sequence[TraceEvent],
    name: str,
) -> tuple[TraceEvent | None, str | None]:
    rows = [event for event in events if event.name == name]
    if len(rows) > 1:
        return None, "trace_point_duplicated"
    if not rows:
        return None, "trace_point_unavailable"
    event = rows[0]
    if event.outcome != "success":
        return None, "trace_point_not_successful"
    if event.clock_domain != "client_monotonic" or event.unit != "millisecond":
        return None, "trace_clock_mismatch"
    try:
        number(event.timestamp)
    except ValueError:
        return None, "trace_timestamp_invalid"
    return event, None


def backend_speech_matches(trial: dict[str, Any], decision: dict[str, Any]) -> bool:
    """正式なBE入力とfixtureの対応だけを照合し、BE時刻を発話起点へ流用しない。"""
    utterance = decision.get("utteranceId")
    if (
        not isinstance(utterance, str)
        or not utterance
        or utterance == trial.get("initial_utterance_id")
    ):
        return False
    rows = [
        row
        for row in trial.get("evidence", {}).get("core_events", [])
        if row.get("type") == "speech_started" and row.get("utteranceId") == utterance
    ]
    if len(rows) != 1:
        return False
    row = rows[0]
    if (
        row.get("sessionId") != trial.get("session_id")
        or row.get("responseId") != trial.get("old_response_id")
        or row.get("sampleRate") != 16000
        or row.get("clockDomain") != "server_monotonic"
        or not isinstance(row.get("trackSid"), str)
        or re.fullmatch(r"TR_[A-Za-z0-9_-]{1,100}", row["trackSid"]) is None
    ):
        return False
    if any(
        type(row.get(key)) is not int or not 0 <= row[key] <= 2**53 - 1
        for key in (
            "inputGeneration",
            "startSample",
            "activeEndSample",
            "detectedSample",
        )
    ):
        return False
    if not (
        row["inputGeneration"] > 0
        and row["startSample"] <= row["activeEndSample"] <= row["detectedSample"]
    ):
        return False
    try:
        number(row["serverTimestampMs"])
        source = trial.get(
            "fixture_clock_bounds", trial["evidence"].get("fixture_clock_bounds")
        )
        # 通知の到着を因果関係の確認だけに使う。異なるhostの時計は減算しない。
        return number(row["atMs"]) >= number(source["sourceStart"]["lowerMs"])
    except (KeyError, TypeError, ValueError):
        return False


def fixture_latency(
    events: Sequence[TraceEvent],
    end: str,
    trial: dict[str, Any],
    observed_endpoint: object | None,
) -> tuple[MetricObservation, tuple[float, float] | None]:
    point, reason = client_point(events, end)
    if reason is not None or point is None:
        return MetricObservation.missing(reason or "trace_point_unavailable"), None
    try:
        bounds = trial.get(
            "fixture_clock_bounds",
            trial.get("evidence", {}).get("fixture_clock_bounds"),
        )
        start = bounds["speechStart"]
        # 判定受信traceは整数msへ切り捨てられる。実停止・取消確認はbrowserの高精度観測値を使う。
        endpoint = (
            number(point.timestamp)
            if observed_endpoint is None
            else number(observed_endpoint)
        )
        resolution = 1 if observed_endpoint is None else 0
        lower = endpoint - number(start["upperMs"])
        upper = endpoint + resolution - number(start["lowerMs"])
        if lower < 0 or upper < lower:
            return MetricObservation.missing("fixture_boundary_overlaps_endpoint"), None
        return MetricObservation.measured(upper), (lower, upper)
    except (KeyError, TypeError, ValueError):
        return MetricObservation.missing("fixture_boundary_unavailable"), None


def select_diagnostic_fixtures(
    fixtures: dict[str, Any], cohort: str, count: int, indices: object,
) -> list[dict[str, Any]]:
    """元catalogの順番を保ち、事前記録した小規模診断の選択だけを許可する。"""
    available = [row for row in fixtures["trials"] if row["cohort"] == cohort]
    if indices is None:
        return available[:count]
    if (not isinstance(indices, list) or count > 10 or len(indices) != count
            or any(type(index) is not int or not 1 <= index <= min(100, len(available))
                   for index in indices)
            or len(set(indices)) != count):
        raise ValueError("invalid diagnostic fixture selection")
    return [available[index - 1] for index in indices]


def summarize(
    manifest: dict[str, Any], events: Sequence[TraceEvent], fixtures: dict[str, Any]
) -> dict[str, Any]:
    if (
        manifest.get("measurement_scope") != "labeled_livekit_interruption_diagnostic"
        or manifest.get("cohort") != "take_turn"
    ):
        raise ValueError("take-turn diagnostic manifest required")
    origin = manifest.get("latency_origin", "legacy_client_vad")
    authority = manifest.get("input_authority", "frontend")
    if origin not in {
        "legacy_client_vad",
        "scheduled_fixture_speech_start",
    } or authority not in {"frontend", "backend"}:
        raise ValueError("unsupported latency origin or input authority")
    fixture_origin = origin == "scheduled_fixture_speech_start"
    if fixture_origin and "input_authority" not in manifest:
        raise ValueError("fixture latency origin requires explicit input authority")
    if authority == "backend" and not fixture_origin:
        raise ValueError("backend input requires fixture latency origin")
    definitions = {
        name: ("fixture_speech_start_lower_bound", *definition[1:])
        if fixture_origin and definition[2] == "client_monotonic"
        else definition
        for name, definition in POINTS.items()
    }
    interval_observations: dict[str, list[tuple[float, float]]] = {
        name: []
        for name, definition in POINTS.items()
        if definition[2] == "client_monotonic"
    }
    count = manifest.get("expected_measured")
    trials = manifest.get("trials")
    if (
        type(count) is not int
        or not 1 <= count <= 100
        or not isinstance(trials, list)
        or len(trials) != count
    ):
        raise ValueError("all expected trials must be recorded before reporting")
    selected = select_diagnostic_fixtures(
        fixtures, "take_turn", count, manifest.get("fixture_indices"),
    )
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
            if stage is None and (
                trial.get("cleanup_failed") is True
                or trial.get("audit_cleanup_failed") is True
            ):
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
        overflow = (
            evidence.get("interruptions_overflow") is True
            or evidence.get("core_events_overflow") is True
        )
        speech_matched = authority != "backend" or (
            matched_decision and backend_speech_matches(trial, decision)
        )
        verified_stop = False
        verified_ack = False
        if len(stops) == 1:
            first, _ = client_point(correlated, "speech_started_client")
            stopped, _ = client_point(correlated, "local_playback_stopped")
            acknowledged, _ = client_point(correlated, "server_cancelled_client")
            stop = stops[0]
            source_matched = (
                (
                    stop.get("utteranceId") == decision.get("utteranceId")
                    and stop.get("ambiguousDecision") is False
                    and stop.get("duplicateStop") is False
                )
                if authority == "backend" and matched_decision
                else first is not None
            )
            if stopped is not None and source_matched:
                try:
                    legacy_matches = authority == "backend" or (
                        first is not None
                        and number(stop.get("speechStartedAtMs"))
                        == number(first.timestamp)
                    )
                    verified_stop = (
                        legacy_matches
                        and not overflow
                        and speech_matched
                        and abs(
                            number(stop.get("localPlaybackStoppedAtMs"))
                            - number(stopped.timestamp)
                        )
                        <= 2
                    )
                except ValueError:
                    pass
            if acknowledged is not None and source_matched:
                try:
                    verified_ack = (
                        not overflow
                        and speech_matched
                        and abs(
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
        for name, definition in definitions.items():
            reason = (
                "fixture_injection_unverified"
                if not injected
                else "final_decision_unavailable"
                if not matched_decision
                else "take_turn_not_selected"
                if not take_turn
                else "browser_observations_overflow"
                if overflow
                else "backend_speech_correlation_unavailable"
                if not speech_matched
                else None
            )
            interval = None
            if reason:
                observation = MetricObservation.missing(reason)
            elif fixture_origin and definition[2] == "client_monotonic":
                endpoint = None
                if len(stops) == 1:
                    if name == "local_playback_stop":
                        endpoint = stops[0].get("localPlaybackStoppedAtMs")
                    elif name == "barge_in_cancel_total":
                        endpoint = stops[0].get("cancelConfirmedAtMs")
                observation, interval = fixture_latency(
                    correlated, definition[1], trial, endpoint
                )
            else:
                observation = latency(correlated, definition)
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
            if interval is not None and observation.status == "measured":
                interval_observations[name].append(interval)
    metrics = [
        aggregate_metric(
            name, observations[name], start_point=definition[0], end_point=definition[1]
        ).model_dump(mode="json")
        for name, definition in definitions.items()
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
    if fixture_origin:
        result.update(
            latency_origin=origin,
            input_authority=authority,
            fixture_latency_bounds={
                name: {
                    "measured_count": len(values),
                    "missing_count": count - len(values),
                    **{
                        f"p{percent}_{side}_ms": type7_quantile(
                            [row[index] for row in values], percent / 100
                        )
                        if values
                        else None
                        for percent in (50, 95)
                        for index, side in enumerate(("lower", "upper"))
                    },
                }
                for name, values in interval_observations.items()
            },
        )
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

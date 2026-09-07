"""相槌cohortの誤停止・誤cancelと、旧応答の全出力継続を匿名集計する。"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.livekit_pilot_report import (
    validate_packet_playback_observation,
    validate_playback_completion,
)
from app.voice_baseline import _assert_anonymous, _load_trace
from app.voice_metrics import MetricObservation, TraceEvent, aggregate_metric
from jsonschema import Draft202012Validator
from report_take_turn import STAGES, bounds_valid, number

SOURCE_COUNTS = (
    "response_audio_input_samples",
    "response_audio_captured_samples",
    "response_audio_padding_samples",
)


def preserved_output(trial: dict[str, Any], events: Sequence[TraceEvent]) -> bool:
    """初回packetと全出力を、同じsession・旧応答・初期発話へ結ぶ。"""
    evidence = trial.get("evidence", {})
    response = trial.get("old_response_id")
    cycles = [
        c
        for c in evidence.get("cycles", [])
        if c.get("sessionId") == trial.get("session_id")
        and c.get("responseId") == response
        and c.get("utteranceId") == trial.get("initial_utterance_id")
    ]
    if len(cycles) != 1 or evidence.get("transport_failures") != []:
        return False
    initial = trial.get("initial_playback")
    if not isinstance(initial, dict) or initial.get("response_id") != response:
        return False
    playback = {
        "startedAt": cycles[0].get("startedAt"),
        "track_response_matches": initial.get("track_response_matches"),
        "track_media_observation": initial.get("track_media_observation"),
        "packet_playback_observation": initial.get("packet_playback_observation"),
        "playback_completion": evidence.get("playback_completions", {}).get(response),
    }
    pair = (trial.get("session_id"), trial.get("initial_utterance_id"), response)
    points = {}
    for name in SOURCE_COUNTS:
        matching = [
            e
            for e in events
            if (e.session_id, e.utterance_id, e.response_id) == pair and e.name == name
        ]
        if (
            len(matching) != 1
            or matching[0].clock_domain != "server_monotonic"
            or matching[0].unit != "nanosecond"
        ):
            return False
        points[name] = matching[0]
    try:
        validate_packet_playback_observation(playback)
        validate_playback_completion(playback, points)
        completion = playback["playback_completion"]
        end_at = (
            completion["outputClockPerformanceTime"]
            + (
                completion["lastOutputEndFrame"] / 48000
                - completion["outputClockContextTime"]
            )
            * 1000
        )
        bounds = trial.get("fixture_clock_bounds", evidence.get("fixture_clock_bounds"))
        # 対象音声が終わる前に旧応答が自然終了した試行では、全区間の継続を主張しない。
        return number(cycles[0].get("startedAt")) <= number(
            bounds["speechStart"]["lowerMs"]
        ) and end_at >= number(bounds["speechEnd"]["upperMs"])
    except (KeyError, TypeError, ValueError):
        return False


def summarize(
    manifest: dict[str, Any], events: Sequence[TraceEvent], fixtures: dict[str, Any]
) -> dict[str, Any]:
    if (
        manifest.get("measurement_scope") != "labeled_livekit_interruption_diagnostic"
        or manifest.get("cohort") != "backchannel"
    ):
        raise ValueError("backchannel diagnostic manifest required")
    count, trials = manifest.get("expected_measured"), manifest.get("trials")
    if (
        type(count) is not int
        or not 1 <= count <= 100
        or not isinstance(trials, list)
        or len(trials) != count
    ):
        raise ValueError("all expected trials must be recorded before reporting")
    selected = [t for t in fixtures["trials"] if t["cohort"] == "backchannel"][:count]
    if (
        len(selected) != count
        or [t.get("fixture_sha256") for t in trials]
        != [t["audio_sha256"] for t in selected]
        or any(t.get("cohort") != "backchannel" for t in trials)
    ):
        raise ValueError("labeled fixture coverage or order mismatch")
    identities = [t["session_id"] for t in trials if t.get("session_id")]
    if len(set(identities)) != len(identities):
        raise ValueError("sessions must be independent")
    if len({e.character_id for e in events}) > 1 or any(
        e.measurement_kind != "controlled_baseline" for e in events
    ):
        raise ValueError("trace mixes characters or measurement kinds")
    counts: Counter[str] = Counter()
    stages: Counter[str] = Counter()
    observations = []
    for trial, fixture in zip(trials, selected, strict=True):
        outcome = trial.get("outcome")
        if outcome not in ("success", "failure"):
            raise ValueError("invalid trial outcome")
        counts[outcome] += 1
        if outcome == "failure":
            stage = trial.get(
                "failure_stage",
                "cleanup" if trial.get("cleanup_failed") is True else None,
            )
            if stage not in STAGES | {"cleanup"}:
                raise ValueError("invalid failure stage")
            stages[stage] += 1
        counts["session_end_confirmed"] += trial.get("session_end_confirmed") is True
        injected = bool(
            trial.get("session_id")
            and trial.get("old_response_id")
            and trial.get("initial_utterance_id")
            and trial.get("injection_playback", {}).get("active") is True
            and bounds_valid(trial, fixture)
        )
        counts["verified_injection"] += injected
        evidence = trial.get("evidence", {})
        response = trial.get("old_response_id")
        server_cancelled = bool(response) and any(
            e.get("type") == "response_cancelled"
            and e.get("responseId") == response
            and e.get("sessionId") == trial.get("session_id")
            for e in evidence.get("core_events", [])
        )
        stops = [
            s
            for s in evidence.get("interruptions", [])
            if response and s.get("responseId") == response
        ]
        local_stop = False
        for stop in stops:
            try:
                number(stop.get("localPlaybackStoppedAtMs"))
                local_stop = True
            except ValueError:
                pass
        decision = trial.get("decision")
        matched = (
            isinstance(decision, dict)
            and decision.get("sessionId") == trial.get("session_id")
            and decision.get("responseId") == response
            and decision.get("final") is True
            and decision.get("decision") in {"backchannel", "take_turn", "indeterminate"}
        )
        for label in ("backchannel", "take_turn", "indeterminate"):
            counts[label + "_decision"] += bool(matched and decision.get("decision") == label)
        counts["unverified_final_decision"] += not matched
        counts["observed_server_cancel"] += bool(server_cancelled)
        counts["observed_local_stop"] += local_stop
        if not injected:
            observation = MetricObservation.missing("fixture_injection_unverified")
        elif local_stop or server_cancelled:
            observation = MetricObservation.failed("backchannel_cancelled")
        elif stops:
            observation = MetricObservation.missing("local_stop_observation_invalid")
        elif preserved_output(trial, events):
            observation = MetricObservation.measured(0)
        else:
            observation = MetricObservation.missing(
                "full_response_continuity_unverified"
            )
        observations.append(observation)
    metric = aggregate_metric(
        "backchannel_false_cancel",
        observations,
        unit="count",
        start_point="labeled_backchannel_injected",
        end_point="old_response_full_output_or_cancel",
    ).model_dump(mode="json")
    coverage = (
        metric["missing_count"] == 0
        and len(identities) == count
        and counts["session_end_confirmed"] == count
    )
    result = {
        "scope": "labeled_livekit_backchannel_cohort_report",
        "quantile_method": "hyndman_fan_type_7",
        "cohort": "backchannel",
        "counts": {
            "expected": count,
            "recorded": len(trials),
            "independent_sessions": len(identities),
            **{
                k: counts[k]
                for k in (
                    "success",
                    "failure",
                    "verified_injection",
                    "session_end_confirmed",
                    "backchannel_decision",
                    "take_turn_decision",
                    "indeterminate_decision",
                    "unverified_final_decision",
                    "observed_server_cancel",
                    "observed_local_stop",
                )
            },
        },
        "failure_stages": dict(stages),
        "metrics": [metric],
        "evaluation": {
            "minimum_trials": 100,
            "coverage_complete": coverage,
            "false_cancel_rate_passed": count >= 100
            and coverage
            and metric["failure_count"] * 100 <= count * 2,
        },
    }
    _assert_anonymous(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "trace", "fixtures", "output", "schema"):
        parser.add_argument("--" + name, type=Path, required=True)
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
    for metric in report["metrics"]:
        Draft202012Validator(metric_schema).validate(metric)
    for name, path in {
        "metric_schema": args.schema,
        "labeled_manifest": args.fixtures,
        "raw_manifest": args.manifest,
        "raw_trace": args.trace,
        "reporter": Path(__file__),
        "playback_validator": Path(inspect.getfile(validate_playback_completion)),
        "shared_reporter": Path(__file__).with_name("report_take_turn.py"),
    }.items():
        report[name + "_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with args.output.open("x") as output:
        output.write(
            json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        )
    print(json.dumps({"counts": report["counts"], "evaluation": report["evaluation"]}))


if __name__ == "__main__":
    main()

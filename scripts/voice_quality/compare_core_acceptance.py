"""#3の同条件pilot比較と#540差分を匿名集計する。品質全体の合格判定は行わない。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.voice_resource_metrics import aggregate_resources
from run_normal_cohort import preparation_summary


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def distribution(values):
    """既存指標と同じHyndman–Fan type 7。空集合は欠測のまま残す。"""
    ordered = sorted(values)
    def percentile(q):
        if not ordered:
            return None
        position = (len(ordered) - 1) * q
        lo = math.floor(position)
        hi = math.ceil(position)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)
    return {"count": len(values), "min": min(values, default=None),
            "p50": percentile(.5), "p95": percentile(.95), "max": max(values, default=None)}


def playback_deltas(events):
    """値はFE−BEの区間番号差。負値が過大推定。msへ変換しない。"""
    grouped = {kind: [] for kind in ("completed", "stopped", "output_stop")}
    seen = set()
    for event in events:
        name = event.get("name", "")
        if not name.startswith("playback_estimate_delta_"):
            continue
        kind = name.removeprefix("playback_estimate_delta_")
        identity = (event.get("session_id"), event.get("response_id"), kind)
        value = event.get("value")
        if (kind not in grouped or not all(identity) or identity in seen
                or event.get("outcome") != "success" or not number(value) or int(value) != value):
            raise ValueError("invalid or duplicate playback delta")
        seen.add(identity)
        grouped[kind].append(value)
    values = [v for group in grouped.values() for v in group]
    over = sum(v < 0 for v in values)
    return {"status": "FAIL" if over else "OBSERVED_ZERO_OVERESTIMATES" if values else "INCONCLUSIVE",
            "unit": "audio_sequence_difference", "observations": len(values),
            "overestimates": over, "by_kind": {k: distribution(v) for k, v in grouped.items()},
            "short_side": distribution([v for v in values if v > 0])}


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def summarize(directory):
    manifest_path = directory / "trial-manifest.json"
    if not manifest_path.exists():
        return {"status": "NOT_RUN", "reason": "trial_manifest_unavailable"}
    manifest = json.loads(manifest_path.read_text())
    revision = manifest.get("measurement_revision", "")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("full measurement revision required")
    expected = manifest.get("expected_measured")
    if type(expected) is not int or expected <= 0:
        raise ValueError("positive planned trial count required")
    trials = [t for t in manifest["trials"] if t.get("phase") == "measured"]
    if len(trials) > expected:
        raise ValueError("unexpected extra trials")
    identities = [(t.get("sessionId"), t.get("responseId")) for t in trials if t.get("responseId")]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate trial identity")
    trace_path = directory / "runtime-data/voice-metrics/controlled-trace.jsonl"
    events = read_jsonl(trace_path) if trace_path.exists() else []
    text, playback = [], []
    for trial in trials:
        matched = [e for e in events if (e.get("session_id"), e.get("response_id")) ==
                   (trial.get("sessionId"), trial.get("responseId")) and e.get("outcome") == "success"]
        def point(name, observations=matched):
            points = [e for e in observations if e.get("name") == name]
            return points[0] if len(points) == 1 else None
        start, end = point("stt_completed"), point("first_text_delta")
        if (start and end and start.get("clock_domain") == end.get("clock_domain") == "server_monotonic"
                and start.get("unit") == end.get("unit") == "nanosecond"
                and number(start.get("timestamp")) and number(end.get("timestamp"))
                and end["timestamp"] >= start["timestamp"]):
            text.append((end["timestamp"] - start["timestamp"]) / 1_000_000)
        end = point("first_playback")
        origin, played = trial.get("fixture_speech_end_client_ms"), trial.get("startedAt")
        bounds = (trial.get("fixture_clock_bounds") or {}).get("speechEnd", {})
        lower, upper = bounds.get("lowerMs"), bounds.get("upperMs")
        if (trial.get("outcome") == "success" and trial.get("transcript_matches") is True
                and trial.get("first_playback_method") == "audio_worklet_output_timestamp"
                and trial.get("fixture_clock_method") == "audio_worklet_pcm_causal_bounds"
                and trial.get("media_observation_method") == "response_track_stateful_opus_worklet_output"
                and trial.get("session_end_confirmed") is True
                and number(lower) and number(upper) and 0 <= upper - lower <= 20 and origin == lower
                and number(origin) and number(played) and played >= origin and end
                and end.get("clock_domain") == "client_monotonic" and end.get("unit") == "millisecond"
                and number(end.get("timestamp")) and abs(end["timestamp"] - played) < 1.1):
            playback.append(played - origin)
    resources_path = directory / "inference-runtime.jsonl"
    resources = aggregate_resources(read_jsonl(resources_path) if resources_path.exists() else []).model_dump(mode="json")
    return {"status": "MEASURED" if len(text) == len(playback) == expected else "INCONCLUSIVE",
            "revision": revision, "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "fixture_sha256": manifest.get("fixture", {}).get("audio_sha256"),
            "initial_state_hash": manifest.get("initial_state_hash"),
            "expected": expected, "recorded": len(trials),
            "succeeded": sum(t.get("outcome") == "success" for t in trials),
            "failed": sum(t.get("outcome") == "failure" for t in trials),
            "unrecorded": expected - len(trials),
            "first_text_after_stt_ms": {**distribution(text), "missing": expected - len(text)},
            "fixture_end_to_playback_ms": {**distribution(playback), "missing": expected - len(playback)},
            "preparation": preparation_summary(trials),
            "warmup_preparation": preparation_summary([
                t for t in manifest["trials"] if t.get("phase") == "warmup"]),
            "resources": resources,
            "playback_estimate": playback_deltas(events)}


def compare(before, after):
    # 数値比較だけ。環境条件・全100試行の受入は記録と既存reporterで別途検証する。
    comparable = (before.get("status") == after.get("status") == "MEASURED"
                  and all(before.get(k) and before.get(k) == after.get(k)
                          for k in ("fixture_sha256", "initial_state_hash", "expected")))
    result = {"schema_version": "1.0",
              "status": "NUMERICAL_COMPARISON" if comparable else "INCONCLUSIVE",
              "acceptance": "INCONCLUSIVE", "before": before, "after": after,
              "quantile_method": "hyndman_fan_type_7"}
    if comparable:
        result["p95_change_ms"] = {k: after[k]["p95"] - before[k]["p95"] for k in
                                   ("first_text_after_stt_ms", "fixture_end_to_playback_ms")}
        result["playback_target_ms"] = 2000
        result["after_playback_target_exceeded"] = after["fixture_end_to_playback_ms"]["p95"] > 2000
        result["resource_change"] = {}
        for name in ("cpu_percent", "memory_bytes"):
            old, new = before["resources"][name], after["resources"][name]
            result["resource_change"][name] = (new["value"] - old["value"]
                if old["status"] == new["status"] == "measured" else None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--delta-trace", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.delta_trace:
        if args.before or args.after:
            parser.error("delta trace and comparison are separate modes")
        result = playback_deltas(read_jsonl(args.delta_trace))
    else:
        if not args.before or not args.after:
            parser.error("both run directories are required")
        result = compare(summarize(args.before), summarize(args.after))
    with args.output.open("x") as output:
        output.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return 1 if result["status"] in {"FAIL", "INCONCLUSIVE"} else 0


if __name__ == "__main__":
    raise SystemExit(main())

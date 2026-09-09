"""実ブラウザのVADイベントをfixtureの因果境界と比較する。PCM取込の証明とは分ける。"""
from __future__ import annotations

import json
import math
from collections import Counter
from hashlib import sha256
from typing import Any
from uuid import UUID

COHORTS = {"backchannel", "take_turn", "pause"}
MISSING = {
    "fixture_boundary_unavailable", "detector_events_unavailable",
    "detector_event_overflow", "speech_not_confirmed", "speech_end_unavailable",
    "detector_event_order_invalid",
    "detector_utterance_correlation_unavailable",
}


def number(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid numeric evidence")
    return float(value)


def distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "minimum_ms": None, "p50_ms": None,
                "p95_ms": None, "maximum_ms": None}
    ordered = sorted(values)

    def quantile(q: float) -> float:
        at = (len(ordered) - 1) * q
        low, high = math.floor(at), math.ceil(at)
        return ordered[low] + (ordered[high] - ordered[low]) * (at - low)

    return {"count": len(ordered), "minimum_ms": ordered[0],
            "p50_ms": quantile(.5), "p95_ms": quantile(.95), "maximum_ms": ordered[-1]}


def measure(trial: dict[str, Any], fixture: dict[str, Any]) -> dict[str, Any]:
    bounds = trial.get("fixture_clock_bounds", trial.get("evidence", {}).get("fixture_clock_bounds"))
    samples = [0, fixture["speech_intervals"][0]["start_sample"],
               fixture["speech_intervals"][-1]["end_sample"]]
    try:
        clocks = []
        for key, sample in zip(("sourceStart", "speechStart", "speechEnd"), samples, strict=True):
            row = bounds[key]
            if type(row["sourceSample"]) is not int or row["sourceSample"] != sample:
                raise ValueError("fixture sample mismatch")
            low, high = number(row["lowerMs"]), number(row["upperMs"])
            if not 0 <= high - low <= 20:
                raise ValueError("fixture clock uncertainty")
            clocks.append((low, high))
        if any(clocks[i][0] > clocks[i + 1][0] or clocks[i][1] > clocks[i + 1][1] for i in (0, 1)):
            raise ValueError("fixture clock order")
    except (TypeError, KeyError, ValueError):
        return {"missing": "fixture_boundary_unavailable"}
    vad = trial.get("evidence", {}).get("vad", {})
    events = vad.get("events")
    if not isinstance(events, list) or not events:
        return {"missing": "detector_events_unavailable"}
    # 旧harnessは128件で先頭を捨てる。上限到達は完全な証跡とみなさない。
    if len(events) >= 128 or vad.get("eventOverflow") is True:
        return {"missing": "detector_event_overflow"}
    try:
        previous = -1.0
        for event in events:
            start, detected = number(event["speechStartedAtMs"]), number(event["detectedAtMs"])
            if event["type"] not in {"candidate", "confirmed", "ended", "misfire"} or start > detected or detected < previous:
                raise ValueError("detector event order")
            previous = detected
    except (TypeError, KeyError, ValueError):
        return {"missing": "detector_event_order_invalid"}
    confirmed = [e for e in events if e["type"] == "confirmed" and e["detectedAtMs"] >= clocks[0][1]]
    if not confirmed:
        return {"missing": "speech_not_confirmed"}
    # 1 frame以上前から続く候補を今回のfixtureの発話開始に取り違えない。
    if any(e["speechStartedAtMs"] < clocks[0][0] - 96 for e in confirmed):
        return {"missing": "detector_utterance_correlation_unavailable"}
    ends = []
    for event in confirmed:
        matches = [e for e in events if e["type"] == "ended"
                   and e["speechStartedAtMs"] == event["speechStartedAtMs"]
                   and e["detectedAtMs"] >= event["detectedAtMs"]]
        if len(matches) != 1:
            return {"missing": "speech_end_unavailable"}
        ends.append(matches[0])
    if len({e["speechStartedAtMs"] for e in confirmed}) != len(confirmed):
        return {"missing": "detector_event_order_invalid"}
    first_start = confirmed[0]["speechStartedAtMs"]
    first_end = ends[0]["detectedAtMs"]
    # 区間が閾値をまたぐ場合も安全側の上限で数える。最終endだけを採って早い分割を隠さない。
    leading = (first_start - clocks[1][1], first_start - clocks[1][0])
    ending = (first_end - clocks[2][1], first_end - clocks[2][0])
    return {"onset_offset_lower_ms": leading[0], "onset_offset_upper_ms": leading[1],
            "end_offset_lower_ms": ending[0], "end_offset_upper_ms": ending[1],
            "leading_error": leading[1] > 100, "early_end_error": ending[0] < -100,
            "boundary_uncertain": leading[0] <= 100 < leading[1] or ending[0] < -100 <= ending[1],
            "split": len(confirmed) > fixture["expected_utterances"]}


def summarize(manifest_bytes: bytes, fixture_bytes: bytes) -> dict[str, Any]:
    manifest, fixtures = json.loads(manifest_bytes), json.loads(fixture_bytes)
    cohort, count, trials = manifest.get("cohort"), manifest.get("expected_measured"), manifest.get("trials")
    if (manifest.get("measurement_scope") not in {"labeled_livekit_interruption_diagnostic", "labeled_livekit_vad_diagnostic"}
            or cohort not in COHORTS or type(count) is not int or not 1 <= count <= 100
            or not isinstance(trials, list) or len(trials) != count
            or manifest.get("labeled_manifest_sha256") != sha256(fixture_bytes).hexdigest()):
        raise ValueError("invalid labeled browser manifest")
    available = [f for f in fixtures["trials"] if f["cohort"] == cohort]
    indices = manifest.get("fixture_indices")
    if indices is not None:
        if (not isinstance(indices, list) or count > 10 or len(indices) != count
                or any(type(index) is not int or not 1 <= index <= min(100, len(available)) for index in indices)
                or len(set(indices)) != count):
            raise ValueError("invalid diagnostic fixture selection")
        selected = [available[index - 1] for index in indices]
    else:
        selected = available[:count]
    if len(selected) != count:
        raise ValueError("fixture cohort incomplete")
    sessions = set()
    missing: Counter[str] = Counter()
    measured = []
    cleanup = 0
    for trial, fixture in zip(trials, selected, strict=True):
        if trial.get("cohort") != cohort or trial.get("fixture_sha256") != fixture["audio_sha256"]:
            raise ValueError("fixture correlation mismatch")
        session = trial.get("session_id")
        if not isinstance(session, str) or str(UUID(session)) != session or session in sessions:
            raise ValueError("independent session identity unavailable")
        sessions.add(session)
        cleanup += trial.get("session_end_confirmed") is True
        if (type(fixture.get("expected_utterances")) is not int or fixture["expected_utterances"] != 1
                or type(fixture.get("sample_rate_hz")) is not int or fixture["sample_rate_hz"] != 48000):
            raise ValueError("unsupported fixture definition")
        intervals = fixture.get("speech_intervals")
        if not isinstance(intervals, list) or len(intervals) != (2 if cohort == "pause" else 1):
            raise ValueError("unsupported fixture intervals")
        for interval in intervals:
            if (type(interval.get("start_sample")) is not int or type(interval.get("end_sample")) is not int
                    or not 0 <= interval["start_sample"] < interval["end_sample"]):
                raise ValueError("invalid fixture interval")
        if cohort == "pause":
            pause = fixture.get("pause_samples")
            if (type(pause) is not int or not 0 < pause <= 28800
                    or intervals[1]["start_sample"] - intervals[0]["end_sample"] != pause):
                raise ValueError("pause outside acceptance scope")
        row = measure(trial, fixture)
        if "missing" in row:
            missing[row["missing"]] += 1
        else:
            measured.append(row)
    counts = {key: sum(row[key] for row in measured)
              for key in ("leading_error", "early_end_error", "boundary_uncertain", "split")}
    coverage = len(measured) == count and cleanup == count
    gates = {"independent_100": count == 100, "coverage_complete": coverage,
             "leading_error_at_most_one_percent": counts["leading_error"] * 100 <= count,
             "early_end_error_at_most_one_percent": counts["early_end_error"] * 100 <= count,
             "split_at_most_one_percent": counts["split"] * 100 <= count}
    return {"schema_version": "1.0", "measurement_scope": "real_browser_vad_detector_boundaries",
            "cohort": cohort, "raw_sha256": sha256(manifest_bytes).hexdigest(),
            "fixture_manifest_sha256": sha256(fixture_bytes).hexdigest(),
            "clock_domain": "browser_monotonic", "maximum_fixture_uncertainty_ms": 20,
            "expected_trials": count, "measured_trials": len(measured), "session_cleanup_confirmed": cleanup,
            "missing": dict(sorted(missing.items())), "counts": counts,
            "metrics": {key: distribution([row[key] for row in measured]) for key in (
                "onset_offset_lower_ms", "onset_offset_upper_ms", "end_offset_lower_ms", "end_offset_upper_ms")},
            "evaluation": {"passed": all(gates.values()), "gates": gates},
            "limits": {"captured_pcm_boundary_verified": False,
                       "intentional_pause_cohort": cohort == "pause", "all_vad_acceptance_verified": False}}



def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError, ValidationError

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "fixtures", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--schema", type=Path, default=Path(__file__).resolve().parents[2]
                        / "docs/schemas/voice-quality-vad-report-v1.schema.json")
    args = parser.parse_args(argv)
    try:
        report = summarize(args.manifest.read_bytes(), args.fixtures.read_bytes())
        schema = json.loads(args.schema.read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(report)
        serialized = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        with args.output.open("x") as output:
            output.write(serialized)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, SchemaError, ValidationError):
        # 入力、schema例外、path、session IDを標準出力へ転記しない。
        print("VAD集計を作成できませんでした。入力証跡と出力先を確認してください。")
        return 2
    print(json.dumps({"expected_trials": report["expected_trials"],
                      "measured_trials": report["measured_trials"],
                      "counts": report["counts"], "evaluation": report["evaluation"]}))
    return 0 if report["evaluation"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

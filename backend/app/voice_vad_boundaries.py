"""通常fixtureのVAD検出境界をnative traceから集計する。PCM取込とは区別する。"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from typing import Literal, Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.livekit_pilot_report import _validate_fixture_clock
from app.voice_baseline import _assert_anonymous
from app.voice_metrics import TraceEvent, type7_quantile


class OffsetDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    count: int = Field(ge=0)
    minimum_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    maximum_ms: float | None


def distribution(values: list[float]) -> OffsetDistribution:
    return OffsetDistribution(count=len(values), minimum_ms=min(values) if values else None,
        p50_ms=type7_quantile(values, .5) if values else None,
        p95_ms=type7_quantile(values, .95) if values else None,
        maximum_ms=max(values) if values else None)


class VadBoundaryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    measurement_kind: Literal["controlled_baseline"] = "controlled_baseline"
    measurement_revision: str = Field(pattern="^[a-f0-9]{40}$")
    scope: Literal["normal_native_vad_detector_offsets_only"] = "normal_native_vad_detector_offsets_only"
    method: Literal["client_detector_timestamps_vs_fixture_causal_bounds_v1"] = "client_detector_timestamps_vs_fixture_causal_bounds_v1"
    warmup_trials: int
    expected_trials: int
    measured_trials: int
    missing_trials: int
    missing_reasons: dict[str, int]
    onset_offset_lower: OffsetDistribution
    onset_offset_upper: OffsetDistribution
    end_offset_lower: OffsetDistribution
    end_offset_upper: OffsetDistribution
    maximum_fixture_uncertainty_ms: float | None
    native_timestamp_quantization_ms: Literal[1] = 1
    leading_error_trials: int
    early_end_error_trials: int
    threshold_uncertain_trials: int
    rate_denominator: int
    boundary_targets_passed: bool
    independent_100_trials_verified: bool
    pcm_capture_boundaries_verified: Literal[False] = False
    false_split_rate_verified: Literal[False] = False
    full_issue_acceptance_verified: Literal[False] = False
    source_sha256: dict[str, str] = Field(default_factory=dict)


def summarize(manifest: dict, events: Sequence[TraceEvent]) -> VadBoundaryReport:
    expected, warmup = manifest.get("expected_measured"), manifest.get("expected_warmup")
    trials = manifest.get("trials")
    if (type(expected) is not int or not 1 <= expected <= 100 or type(warmup) is not int
            or warmup != (5 if expected == 100 else 1) or not isinstance(trials, list)
            or len(trials) > expected + warmup):
        raise ValueError("invalid normal trial population")
    revision = manifest.get("measurement_revision")
    if not isinstance(revision, str):
        raise ValueError("measurement revision required")
    fixture = manifest.get("fixture")
    if not isinstance(fixture, dict):
        raise ValueError("normal fixture required")
    def integer(key: str) -> int:
        value = fixture.get(key)
        if type(value) is not int:
            raise ValueError("invalid fixture samples")
        return value
    sr, start, end = (integer(key) for key in ("sample_rate_hz", "speech_start_sample", "speech_end_sample"))
    if not (sr > 0 and 0 <= start < end):
        raise ValueError("invalid fixture samples")
    if len({event.character_id for event in events}) > 1:
        raise ValueError("mixed character population")
    grouped: dict[tuple[str, str, str], list[TraceEvent]] = {}
    for event in events:
        if event.measurement_kind != "controlled_baseline":
            raise ValueError("measurement kind mismatch")
        grouped.setdefault((event.session_id, event.utterance_id, event.response_id), []).append(event)
    identities: dict[str, set[str]] = {name: set() for name in ("sessionId", "conversationId", "utteranceId", "responseId")}
    for index, trial in enumerate(trials):
        if not isinstance(trial, dict) or trial.get("phase") != ("warmup" if index < warmup else "measured"):
            raise ValueError("invalid normal trial phase")
        if trial.get("audio_sha256") != fixture.get("audio_sha256") or trial.get("fixture_version") != fixture.get("fixture_version"):
            raise ValueError("fixture identity mismatch")
        for name, seen in identities.items():
            value = trial.get(name)
            if not isinstance(value, str) or str(UUID(value)) != value or value in seen:
                raise ValueError("independent trial identity required")
            seen.add(value)
    missing: Counter[str] = Counter()
    values: dict[str, list[float]] = {key: [] for key in ("onset_lower", "onset_upper", "end_lower", "end_upper")}
    uncertainties: list[float] = []
    leading = early = uncertain = 0
    for index in range(warmup, expected + warmup):
        if index >= len(trials):
            missing["trial_not_recorded"] += 1
            continue
        trial = trials[index]
        if trial.get("outcome") != "success" or trial.get("transcript_matches") is not True or trial.get("session_end_confirmed") is not True:
            missing["trial_not_completed"] += 1
            continue
        try:
            _validate_fixture_clock(trial, sample_rate=sr, start_sample=start, end_sample=end, controlled=True)
        except (ValueError, TypeError, KeyError):
            missing["fixture_boundary_unavailable"] += 1
            continue
        rows = grouped.get((trial["sessionId"], trial["utteranceId"], trial["responseId"]), [])
        selected = [[row for row in rows if row.name == name] for name in ("vad_speech_start_client", "speech_stopped")]
        if any(len(rows) != 1 for rows in selected):
            missing["native_boundary_missing_or_duplicate"] += 1
            continue
        onset, stopped = [rows[0] for rows in selected]
        if any(row.outcome != "success" or row.clock_domain != "client_monotonic" or row.unit != "millisecond"
               or not math.isfinite(row.timestamp) or row.timestamp < 0 or row.timestamp % 1 for row in (onset, stopped)) or stopped.timestamp < onset.timestamp:
            missing["native_boundary_clock_or_order_invalid"] += 1
            continue
        bounds = trial["fixture_clock_bounds"]
        lo, hi = onset.timestamp - bounds["speechStart"]["upperMs"], onset.timestamp + 1 - bounds["speechStart"]["lowerMs"]
        elo, ehi = stopped.timestamp - bounds["speechEnd"]["upperMs"], stopped.timestamp + 1 - bounds["speechEnd"]["lowerMs"]
        for name, value in zip(values, (lo, hi, elo, ehi), strict=True):
            values[name].append(value)
        uncertainties.extend(bounds[name]["upperMs"] - bounds[name]["lowerMs"] for name in ("speechStart", "speechEnd"))
        leading += hi > 100
        early += elo < -100
        uncertain += lo <= 100 < hi or elo < -100 <= ehi
    measured = len(values["onset_lower"])
    return VadBoundaryReport(measurement_revision=revision, warmup_trials=warmup,
        expected_trials=expected, measured_trials=measured, missing_trials=expected-measured,
        missing_reasons=dict(missing), onset_offset_lower=distribution(values["onset_lower"]),
        onset_offset_upper=distribution(values["onset_upper"]), end_offset_lower=distribution(values["end_lower"]),
        end_offset_upper=distribution(values["end_upper"]), maximum_fixture_uncertainty_ms=max(uncertainties) if uncertainties else None,
        leading_error_trials=leading, early_end_error_trials=early, threshold_uncertain_trials=uncertain,
        rate_denominator=expected, independent_100_trials_verified=expected == measured == 100,
        boundary_targets_passed=expected == measured == 100 and leading * 100 <= expected and early * 100 <= expected and uncertain == 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest_bytes, trace_bytes = args.manifest.read_bytes(), args.trace.read_bytes()
    manifest = json.loads(manifest_bytes)
    root = Path(__file__).resolve().parents[2]
    fixture = json.loads((root / "frontend/playwright/fixtures/speech.metadata.json").read_text())
    if manifest.get("fixture") != {key: fixture[key] for key in ("fixture_version", "audio_sha256", "sample_rate_hz", "speech_start_sample", "speech_end_sample")}:
        raise ValueError("repository fixture identity mismatch")
    result = summarize(manifest, [TraceEvent.model_validate_json(line) for line in trace_bytes.splitlines() if line.strip()])
    result = result.model_copy(update={"source_sha256": {"trial_manifest": hashlib.sha256(manifest_bytes).hexdigest(),
                                                       "native_trace": hashlib.sha256(trace_bytes).hexdigest()}})
    value = result.model_dump(mode="json")
    _assert_anonymous(value)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

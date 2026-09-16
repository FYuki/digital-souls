"""通常応答の失敗を分母に残し、検証できた試行の時間だけを診断集計する。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from app.livekit_pilot_report import (
    _validate_fixture_clock,
    _validate_initial_state_evidence,
    validate_packet_playback_observation,
    validate_playback_completion,
)
from app.voice_baseline import _assert_anonymous, _load_trace
from app.voice_metrics import (
    _LIVEKIT_DIAGNOSTIC_CATALOG,
    _LIVEKIT_VAD_EVENTS,
    _METRIC_CATALOG,
    LIVEKIT_VAD_POINTS,
    MetricObservation,
    TraceEvent,
    _metric_observation,
    aggregate_metric,
)
from jsonschema import Draft202012Validator

NAMES = {
    "ttfa", "stt_start_latency", "stt_processing", "llm_start_latency",
    "first_text_latency", "llm_completion", "tts_start_latency",
    "first_audio_generation", "client_playback_latency", "client_decode_latency",
    "decoded_to_playback_latency", "llm_first_token_latency",
    "llm_capacity_wait", "llm_http_headers_latency", "prompt_preparation",
    "vad_leading_boundary", "vad_trailing_boundary", "processing_failure",
}

def definitions():
    return [
        replace(d, start_point=LIVEKIT_VAD_POINTS[d.name][0],
                end_event=_LIVEKIT_VAD_EVENTS[d.name], end_point=LIVEKIT_VAD_POINTS[d.name][1])
        if d.name in LIVEKIT_VAD_POINTS else d
        for d in (*_METRIC_CATALOG, *_LIVEKIT_DIAGNOSTIC_CATALOG) if d.name in NAMES
    ]

def observations(trial, trace, fixture, initial_hash):
    catalog = definitions()
    if trial.get("outcome") == "failure":
        return {d.name: (MetricObservation.failed("voice_cycle_incomplete")
                        if d.name == "processing_failure"
                        else MetricObservation.missing("failed_voice_trial")) for d in catalog}, "voice_cycle_incomplete"
    try:
        if trial.get("outcome") != "success" or trial.get("transcript_matches") is not True or trial.get("session_end_confirmed") is not True:
            raise ValueError("trial confirmation unavailable")
        if trial.get("initial_state_hash") != initial_hash:
            raise ValueError("initial state differs")
        for name in ["fixture_version", "audio_sha256"]:
            if trial.get(name) != fixture[name]:
                raise ValueError("fixture mismatch")
        pair = tuple(trial[name] for name in ["sessionId", "utteranceId", "responseId"])
        for value in (*pair, trial["conversationId"]):
            UUID(value)
        matched = [e for e in trace if (e.session_id, e.utterance_id, e.response_id) == pair]
        if not matched or any(e.measurement_kind != "controlled_baseline" or e.outcome != "success" for e in matched):
            raise ValueError("trace outcome unavailable")
        if len({e.character_id for e in matched}) != 1:
            raise ValueError("mixed character evidence")
        points = {e.name: e for e in matched}
        for name in ["stt_completed", "llm_completed", "tts_pipeline_completed", "first_playback"]:
            if name not in points:
                raise ValueError("generation or playback unavailable")
        playback = trial["startedAt"]
        if isinstance(playback, bool) or not isinstance(playback, (int, float)) or not math.isfinite(playback) or playback < 0:
            raise ValueError("invalid playback clock")
        event = points["first_playback"]
        if event.clock_domain != "client_monotonic" or event.unit != "millisecond" or abs(event.timestamp - playback) >= 1.1:
            raise ValueError("playback clock mismatch")
        _validate_initial_state_evidence(trial)
        validate_packet_playback_observation(trial)
        validate_playback_completion(trial, points)
        start, end = _validate_fixture_clock(trial, sample_rate=fixture["sample_rate_hz"],
            start_sample=fixture["speech_start_sample"], end_sample=fixture["speech_end_sample"], controlled=True)
        for name, timestamp in [("fixture_speech_start", start), ("fixture_speech_end", end)]:
            if name in points:
                raise ValueError("unexpected preexisting fixture event")
            matched.append(TraceEvent(schema_version="1.0", measurement_kind="controlled_baseline",
                event_id=f"diagnostic-{name}", character_id=matched[0].character_id,
                session_id=pair[0], utterance_id=pair[1], response_id=pair[2], name=name,
                stage="fixture", outcome="success", timestamp=timestamp,
                clock_domain="client_monotonic", unit="millisecond"))
        return {d.name: _metric_observation(d, matched, transport="livekit") for d in catalog}, None
    except (ValueError, TypeError, KeyError):
        return {d.name: MetricObservation.missing("trial_evidence_invalid") for d in catalog}, "trial_evidence_invalid"

def summarize(manifest, trace):
    trials = manifest["trials"]
    if manifest.get("measurement_scope") != "controlled" or manifest.get("expected_warmup") != 5 or manifest.get("expected_measured") != 100 or len(trials) != 105:
        raise ValueError("all 105 isolated trials required")
    if [t.get("phase") for t in trials] != ["warmup"] * 5 + ["measured"] * 100:
        raise ValueError("trial phases differ")
    if any(t.get("outcome") not in {"success", "failure"} for t in trials):
        raise ValueError("invalid trial outcome")
    successful = [t for t in trials if t.get("outcome") == "success"]
    if any(len({t.get(key) for t in successful}) != len(successful)
           for key in ["sessionId", "utteranceId", "responseId", "conversationId"]):
        raise ValueError("independent trial identities required")
    identities = [tuple(t.get(k) for k in ["sessionId", "utteranceId", "responseId"]) for t in trials if t.get("outcome") == "success"]
    if len(set(identities)) != len(identities):
        raise ValueError("trial identities reused")
    fixture = manifest["fixture"]
    measured = trials[5:]
    rows = [observations(t, trace, fixture, manifest["initial_state_hash"]) for t in measured]
    metrics = [aggregate_metric(d.name, [row[d.name] for row, _ in rows],
        unit=d.unit, start_point=d.start_point, end_point=d.end_point).model_dump(mode="json") for d in definitions()]
    result = {
        "scope": "isolated_normal_diagnostic_with_failed_trials",
        "quantile_method": "hyndman_fan_type_7",
        "measurement_revision": manifest["measurement_revision"],
        "counts": {"expected_measured": 100, "recorded_measured": len(measured),
                   "warmup_recorded": 5, "warmup_success": sum(t.get("outcome") == "success" for t in trials[:5]),
                   "success": sum(t.get("outcome") == "success" for t in measured),
                   "failure": sum(t.get("outcome") == "failure" for t in measured),
                   "session_end_confirmed": sum(t.get("session_end_confirmed") is True for t in measured),
                   "validated_success": sum(reason is None for _, reason in rows)},
        "evidence_reasons": dict(Counter(reason for _, reason in rows if reason is not None)),
        "metrics": metrics, "acceptance_passed": False,
        "interpretation": "時間分布は成功かつ証跡検証済みの試行から算出し、失敗・欠測も分母100に保持する。既存受入reporterの拒否を上書きしない。",
    }
    _assert_anonymous(result)
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest_path = args.cohort_root / "trial-manifest.json"
    trace_path = args.cohort_root / "controlled-trace.jsonl"
    manifest = json.loads(manifest_path.read_text())
    repository = Path.cwd()
    reference_path = repository / "frontend/playwright/fixtures/speech.metadata.json"
    reference = json.loads(reference_path.read_text())
    fixture_audio = repository / "frontend/playwright/fixtures/speech.wav"
    if hashlib.sha256(fixture_audio.read_bytes()).hexdigest() != reference["audio_sha256"]:
        raise ValueError("repository fixture hash mismatch")
    for key in ["fixture_version", "audio_sha256", "sample_rate_hz", "speech_start_sample", "speech_end_sample"]:
        if manifest["fixture"].get(key) != reference[key]:
            raise ValueError("fixture differs from repository")
    result = summarize(manifest, _load_trace(trace_path))
    schema_path = repository / "docs/schemas/voice-quality-artifact-v1.schema.json"
    schema = json.loads(schema_path.read_text())
    metric_schema = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/metric"}
    Draft202012Validator.check_schema(metric_schema)
    for metric in result["metrics"]:
        Draft202012Validator(metric_schema).validate(metric)
    result["metric_schema_sha256"] = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    result["fixture_metadata_sha256"] = hashlib.sha256(reference_path.read_bytes()).hexdigest()
    for key, path in {"manifest": manifest_path, "trace": trace_path, "reporter": Path(__file__)}.items():
        result[key + "_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with args.output.open("x") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps(result["counts"]))

if __name__ == "__main__":
    main()


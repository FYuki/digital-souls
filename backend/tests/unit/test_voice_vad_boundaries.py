"""正解境界の時計幅、測定分母、実際の取込との区別を検証する。"""
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.voice_metrics import TraceEvent
from app.voice_vad_boundaries import summarize, VadBoundaryReport


def inputs(count=100):
    warmup = 5 if count == 100 else 1
    fixture = dict(fixture_version="test", audio_sha256="a" * 64, sample_rate_hz=48000,
                   speech_start_sample=48000, speech_end_sample=96000)
    trials, events = [], []
    for i in range(count + warmup):
        identity = {key: str(uuid4()) for key in ("sessionId", "conversationId", "utteranceId", "responseId")}
        trials.append(dict(**identity, phase="warmup" if i < warmup else "measured",
            outcome="success", transcript_matches=True, session_end_confirmed=True,
            fixture_version="test", audio_sha256="a" * 64, fixtureStartedAt=1000,
            fixture_speech_end_client_ms=3000, fixture_clock_method="audio_worklet_pcm_causal_bounds",
            fixture_clock_maximum_uncertainty_ms=20,
            fixture_clock_bounds={name: dict(sourceSample=sample, lowerMs=stamp, upperMs=stamp + 2)
                for name, sample, stamp in [("sourceStart",0,1000), ("speechStart",48000,2000),("speechEnd",96000,3000)]}))
        for name, stamp in [("vad_speech_start_client",1990), ("speech_stopped",3100)]:
            events.append(TraceEvent(schema_version="1.0", measurement_kind="controlled_baseline",
                event_id=str(uuid4()), character_id="fixture-persona", session_id=identity["sessionId"],
                utterance_id=identity["utteranceId"], response_id=identity["responseId"], name=name,
                stage="vad", outcome="success", timestamp=stamp, clock_domain="client_monotonic", unit="millisecond"))
    return dict(measurement_revision="b" * 40, fixture=fixture, expected_measured=count,
                expected_warmup=warmup, trials=trials), events


def test_full_native_population_preserves_signed_offsets_and_uncertainty():
    manifest, events = inputs()
    result = summarize(manifest, events)
    assert result.measured_trials == result.rate_denominator == 100
    assert result.missing_trials == 0 and result.boundary_targets_passed
    assert result.onset_offset_lower.p95_ms == -12
    assert result.onset_offset_upper.p95_ms == -9
    assert result.end_offset_lower.p95_ms == 98
    assert result.end_offset_upper.p95_ms == 101
    assert result.maximum_fixture_uncertainty_ms == 2
    assert not result.pcm_capture_boundaries_verified
    assert not result.false_split_rate_verified
    assert not result.full_issue_acceptance_verified
    raw = result.model_dump_json()
    assert "fixture-persona" not in raw
    assert all(trial["sessionId"] not in raw for trial in manifest["trials"])


@pytest.mark.parametrize("endpoint,stamp,field", [("vad_speech_start_client",2101,"leading_error_trials"),
                                                  ("speech_stopped",2901,"early_end_error_trials")])
def test_threshold_crossing_is_counted_conservatively_and_is_not_a_pass(endpoint, stamp, field):
    manifest, events = inputs()
    index = -2 if endpoint == "vad_speech_start_client" else -1
    events[index] = events[index].model_copy(update={"timestamp": stamp})
    result = summarize(manifest, events)
    assert getattr(result,field) == 1
    assert result.threshold_uncertain_trials == 1
    assert not result.boundary_targets_passed


@pytest.mark.parametrize("damage,reason", [
    ("clock", "native_boundary_clock_or_order_invalid"),
    ("order", "native_boundary_clock_or_order_invalid"),
    ("duplicate", "native_boundary_missing_or_duplicate"),
    ("response", "native_boundary_missing_or_duplicate"),
    ("uncertain", "fixture_boundary_unavailable"),
    ("sample", "fixture_boundary_unavailable"),
    ("failed", "trial_not_completed"),
])
def test_missing_evidence_never_becomes_success(damage, reason):
    manifest, events = inputs()
    if damage == "clock": events[-1] = events[-1].model_copy(update={"clock_domain":"server_monotonic"})
    if damage == "order": events[-1] = events[-1].model_copy(update={"timestamp":1900})
    if damage == "duplicate": events.append(events[-1])
    if damage == "response": events[-1] = events[-1].model_copy(update={"response_id":str(uuid4())})
    if damage == "uncertain": manifest["trials"][-1]["fixture_clock_bounds"]["speechEnd"]["upperMs"] = 3021
    if damage == "sample": manifest["trials"][-1]["fixture_clock_bounds"]["speechEnd"]["sourceSample"] += 1
    if damage == "failed": manifest["trials"][-1]["outcome"] = "failure"
    result = summarize(manifest, events)
    assert result.missing_trials == 1 and result.measured_trials == 99
    assert result.missing_reasons == {reason:1}
    assert result.rate_denominator == 100 and not result.boundary_targets_passed


def test_missing_trial_stays_in_denominator_and_warmup_is_excluded():
    manifest, events = inputs()
    events[0] = events[0].model_copy(update={"timestamp":9000})
    manifest["trials"].pop()
    result = summarize(manifest, events)
    assert result.missing_reasons == {"trial_not_recorded":1}
    assert result.leading_error_trials == 0


def test_duplicate_sessions_rejected_and_pilot_is_not_100_acceptance():
    manifest, events = inputs(3)
    assert not summarize(manifest, events).boundary_targets_passed
    manifest["trials"][1]["sessionId"] = manifest["trials"][0]["sessionId"]
    with pytest.raises(ValueError, match="independent"):
        summarize(manifest, events)


def test_schema_matches_model():
    root = Path(__file__).resolve().parents[3]
    assert json.loads((root/"docs/schemas/voice-vad-boundaries-v1.schema.json").read_text()) == VadBoundaryReport.model_json_schema()


def test_character_mixture_and_unrounded_native_timestamp_are_rejected():
    manifest, events = inputs(3)
    events[-1] = events[-1].model_copy(update={"timestamp":3100.5})
    assert summarize(manifest, events).missing_reasons == {"native_boundary_clock_or_order_invalid":1}
    events[-1] = events[-1].model_copy(update={"character_id":"different"})
    with pytest.raises(ValueError, match="mixed character"):
        summarize(manifest, events)

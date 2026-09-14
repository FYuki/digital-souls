from __future__ import annotations

import copy
import importlib
import json
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.unit.test_voice_quality_stt_pcm_report import build_pcm_evidence

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/voice_quality"))
try:
    report = importlib.import_module("report_vad")
finally:
    sys.path.remove(str(ROOT / "scripts/voice_quality"))


def evidence():
    manifest, observed, trace, _ = build_pcm_evidence()
    trial, pcm = manifest["trials"][0], observed[0]
    trace = [e for e in trace if e["session_id"] == trial["session_id"]]
    utterance = trial["evidence"]["core_events"][0]["utteranceId"]
    session, initial = trial["session_id"], trial["initial_utterance_id"]
    def boundary(kind, identity, at, start, active, detected):
        return {"type": kind, "utteranceId": identity, "sessionId": session, "atMs": at,
                "trackSid": "TR_microphone", "inputGeneration": 1, "sampleRate": 16000,
                "clockDomain": "server_monotonic", "serverTimestampMs": 100000 + at,
                "startSample": start, "activeEndSample": active, "detectedSample": detected}
    trial.update(cohort="pause", fixture_clock_bounds={
        key: {"sourceSample": sample, "lowerMs": at, "upperMs": at + 2}
        for key, sample, at in (("sourceStart", 0, 900), ("speechStart", 4800, 1000), ("speechEnd", 48000, 1900))
    })
    trial["evidence"] = {"core_events_overflow": False, "core_events": [
        boundary("speech_started", initial, 100, 0, 900, 1000),
        boundary("speech_started", utterance, 1300, 15120, 19900, 20000),
        boundary("speech_stopped", utterance, 2600, 15120, 29520, 42000),
        {"type": "utterance_finalized", "sessionId": session, "utteranceId": utterance, "atMs": 2800},
    ]}
    values = {
        "stt_capture_media_span_valid": 1, "stt_capture_media_start_sample": 9000,
        "stt_capture_media_end_sample": 42000, "stt_capture_raw_sample_count": 33000,
        "stt_input_raw_sample_count": 33000, "stt_input_removed_prefix_samples": 1000,
        "vad_started_sample": 15120, "vad_active_end_sample": 29520, "vad_detected_end_sample": 42000,
    }
    trace.extend({"session_id": session, "utterance_id": utterance, "name": key, "value": value}
                 for key, value in values.items())
    for e in trace:
        e.update(measurement_kind="controlled_baseline", character_id="private-character")
    pcm["alignment"] = {
        "method": "two_anchor_normalized_pcm_correlation_v1", "status": "aligned", "reason": None,
        "sample_rate_hz": 16000, "captured_sample_count": 32000, "reference_sample_count": 32000,
        "alignment_tolerance_samples": 32, "lag_difference_samples": 0,
        "anchors": copy.deepcopy(pcm["edge_alignment"]["anchors"]),
    }
    fixture = {"audio_sha256": trial["fixture_sha256"], "cohort": "pause",
               "sample_rate_hz": 48000, "expected_utterances": 1, "pause_samples": 9600,
               "trailing_samples": 48000,
               "speech_intervals": [{"start_sample": 4800, "end_sample": 19200},
                                    {"start_sample": 28800, "end_sample": 48000}]}
    fixture_bytes = json.dumps({"trials": [fixture]}).encode()
    manifest.update(trials=[trial], expected_measured=1, input_authority="backend",
                    measurement_scope="labeled_livekit_vad_diagnostic",
                    labeled_manifest_sha256=sha256(fixture_bytes).hexdigest())
    return manifest, fixture_bytes, trace, [pcm]


def summarize(value):
    m, fixtures, trace, observed = value
    result = report.summarize(
        json.dumps(m).encode(), fixtures,
        b"\n".join(json.dumps(e).encode() for e in trace),
        b"\n".join(json.dumps(e).encode() for e in observed),
    )
    schema = json.loads((ROOT / "docs/schemas/voice-quality-vad-report-v1.schema.json").read_text())
    Draft202012Validator(schema).validate(result)
    return result


def test_backend_sample_anchor_and_actual_pcm_measure_offsets_without_clock_subtraction():
    value = evidence()
    result = summarize(value)
    assert result["measured_trials"] == result["split_observed_trials"] == 1
    assert result["clock_domain"] == "vad_media_samples"
    assert result["metrics"]["onset_offset_lower_ms"]["p95_ms"] == -2
    assert result["metrics"]["onset_offset_upper_ms"]["p95_ms"] == 2
    assert result["metrics"]["end_offset_lower_ms"]["p95_ms"] == 778
    assert result["metrics"]["end_offset_upper_ms"]["p95_ms"] == 782
    assert result["counts"] == {"leading_error": 0, "early_end_error": 0, "boundary_uncertain": 0, "split": 0}
    assert not result["evaluation"]["passed"]  # pilotを100試行へ読み替えない。
    for row in value[0]["trials"][0]["evidence"]["core_events"]:
        if "serverTimestampMs" in row:
            row["serverTimestampMs"] += 10**9
    assert summarize(value)["metrics"] == result["metrics"]
    assert "private-character" not in json.dumps(result)
    assert value[0]["trials"][0]["session_id"] not in json.dumps(result)


@pytest.mark.parametrize("damage", [
    "span", "raw_length", "trim", "start_anchor", "end_anchor", "missing_point", "duplicate_point",
    "wrong_generation", "overflow", "snapshot", "active", "v3_edge", "nonuniform",
    "ambiguous_anchor", "wrong_reference_size",
])
def test_backend_incomplete_or_ambiguous_alignment_stays_missing(damage):
    value = evidence()
    m, _, trace, rows = value
    trial, row = m["trials"][0], rows[0]
    points = {e["name"]: e for e in trace}
    if damage == "span":
        points["stt_capture_media_span_valid"]["value"] = 0
    elif damage == "raw_length":
        points["stt_capture_raw_sample_count"]["value"] += 1
    elif damage == "trim":
        points["stt_input_removed_prefix_samples"]["value"] += 1
    elif damage == "start_anchor":
        points["vad_started_sample"]["value"] += 1
    elif damage == "end_anchor":
        points["stt_capture_media_end_sample"]["value"] += 1
    elif damage == "missing_point":
        trace.remove(points["vad_started_sample"])
    elif damage == "duplicate_point":
        trace.append(copy.deepcopy(points["vad_started_sample"]))
    elif damage == "wrong_generation":
        trial["evidence"]["core_events"][2]["inputGeneration"] = 2
    elif damage == "overflow":
        trial["evidence"]["core_events_overflow"] = True
    elif damage == "snapshot":
        trial["pcm_input_observation"]["rows"] = []
    elif damage == "active":
        trial["pcm_input_observation"]["active_requests"] = 1
    elif damage == "v3_edge":
        row["bounded_edge_alignment"]["method"] = "legacy"
    elif damage == "nonuniform":
        row["alignment"]["anchors"][1]["captured_start_sample"] += 33
        row["alignment"]["lag_difference_samples"] = 33
    elif damage == "ambiguous_anchor":
        row["alignment"]["anchors"][0]["competing_peak_correlation"] = .98
    elif damage == "wrong_reference_size":
        row["alignment"]["reference_sample_count"] += 1
    result = summarize(value)
    assert result["expected_trials"] == 1 and result["measured_trials"] == 0
    assert sum(result["missing"].values()) == 1
    assert not result["evaluation"]["passed"]


def test_split_is_counted_even_when_each_partial_pcm_cannot_align_to_the_whole_fixture():
    value = evidence()
    trial = value[0]["trials"][0]
    events = trial["evidence"]["core_events"]
    extra_start, extra_end, extra_final = [copy.deepcopy(e) for e in events[1:]]
    for row in (extra_start, extra_end, extra_final):
        row["utteranceId"] = "second"
        row["atMs"] += 3000
        if "serverTimestampMs" in row:
            row["serverTimestampMs"] += 3000
            for key in ("startSample", "activeEndSample", "detectedSample"):
                row[key] += 50000
    events.extend((extra_start, extra_end, extra_final))
    result = summarize(value)
    assert result["counts"]["split"] == result["split_observed_trials"] == 1
    assert result["split_unknown_trials"] == 0
    assert result["measured_trials"] == 0
    assert result["missing"] == {"backend_split_pcm_alignment_unavailable": 1}
    assert not result["evaluation"]["gates"]["split_at_most_one_percent"]


def test_source_alignment_failure_does_not_hide_confirmed_structure():
    value = evidence()
    value[3][0]["alignment"]["status"] = "unverified"
    result = summarize(value)
    assert result["split_observed_trials"] == 1 and result["split_unknown_trials"] == 0
    assert result["counts"]["split"] == 0
    assert result["missing"] == {"backend_pcm_alignment_unverified": 1}


def test_cli_consumes_trace_and_pcm_observer_and_preserves_pilot_exit(tmp_path):
    manifest, fixtures, trace, observed = evidence()
    inputs = {
        "manifest": json.dumps(manifest).encode(),
        "fixtures": fixtures,
        "trace": b"\n".join(json.dumps(e).encode() for e in trace),
        "pcm-observer": b"\n".join(json.dumps(e).encode() for e in observed),
    }
    args = []
    for key, data in inputs.items():
        path = tmp_path / key
        path.write_bytes(data)
        args.extend(("--" + key, str(path)))
    output = tmp_path / "report.json"
    args.extend(("--output", str(output)))
    assert report.main(args) == 1
    actual = json.loads(output.read_text())
    assert actual["measured_trials"] == 1
    assert actual["trace_sha256"] == sha256(inputs["trace"]).hexdigest()
    assert actual["pcm_observer_sha256"] == sha256(inputs["pcm-observer"]).hexdigest()
    assert actual["evaluation"]["gates"]["coverage_complete"]
    assert not actual["evaluation"]["gates"]["independent_100"]

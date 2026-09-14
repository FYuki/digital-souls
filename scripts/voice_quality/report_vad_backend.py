"""BEの正式sample境界を実Whisper入力と固定PCMへ対応付ける。時計の差は使わない。"""

from __future__ import annotations

import math
import re

from pcm_boundary_alignment import ALIGNMENT_TOLERANCE_SAMPLES, MAX_LAG_DIFFERENCE_SAMPLES
from report_stt_pcm import correlate_final_pcm, valid_edges
from report_take_turn import bounds_valid

MATCH_METHOD = "two_anchor_pcm_with_v4_edge_witness"


def integer(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 2**53 - 1 and value == int(value)


def _boundary(row: dict, session: str) -> bool:
    return (
        row.get("sessionId") == session
        and isinstance(row.get("utteranceId"), str) and bool(row["utteranceId"])
        and isinstance(row.get("trackSid"), str)
        and re.fullmatch(r"TR_[A-Za-z0-9_-]{1,100}", row["trackSid"]) is not None
        and integer(row.get("inputGeneration")) and row["inputGeneration"] > 0
        and row.get("sampleRate") == 16000 and row.get("clockDomain") == "server_monotonic"
        and integer(row.get("serverTimestampMs"))
        and all(integer(row.get(k)) for k in ("startSample", "activeEndSample", "detectedSample"))
        and row["startSample"] <= row["activeEndSample"] <= row["detectedSample"]
    )


def _structure(trial: dict, fixture: dict) -> dict:
    def missing(reason, split=None):
        return {"missing": reason, "split": split}
    evidence = trial.get("evidence", {})
    if evidence.get("core_events_overflow") is not False:
        return missing("backend_event_overflow")
    if not bounds_valid(trial, fixture):
        return missing("fixture_boundary_unavailable")
    events = evidence.get("core_events", [])
    session, initial_id = trial.get("session_id"), trial.get("initial_utterance_id")
    initial = [e for e in events if e.get("type") == "speech_started" and e.get("utteranceId") == initial_id]
    if len(initial) != 1 or not _boundary(initial[0], session):
        return missing("backend_structure_unavailable")
    bounds = trial.get("fixture_clock_bounds", evidence.get("fixture_clock_bounds"))
    lower = bounds["sourceStart"]["lowerMs"]
    formal = [e for e in events if e.get("type") in ("speech_started", "speech_stopped")
              and e.get("utteranceId") != initial_id]
    if any(type(e.get("atMs")) not in (int, float) or not math.isfinite(e["atMs"]) for e in formal):
        return missing("backend_structure_unavailable")
    formal = [e for e in formal if e["atMs"] >= lower]
    if any(not _boundary(e, session) or e["trackSid"] != initial[0]["trackSid"]
           or e["inputGeneration"] != initial[0]["inputGeneration"] for e in formal):
        return missing("backend_structure_unavailable")
    starts = sorted([e for e in formal if e["type"] == "speech_started"], key=lambda e: e["startSample"])
    ends = [e for e in formal if e["type"] == "speech_stopped"]
    ids = [e["utteranceId"] for e in starts]
    if not starts or len(set(ids)) != len(ids) or len(ends) != len(starts):
        return missing("backend_structure_unavailable")
    paired = []
    for start in starts:
        matching = [e for e in ends if e["utteranceId"] == start["utteranceId"]]
        if len(matching) != 1:
            return missing("backend_structure_unavailable")
        end = matching[0]
        if (end["startSample"] != start["startSample"]
                or end["activeEndSample"] < start["activeEndSample"]
                or end["detectedSample"] < start["detectedSample"]
                or end["serverTimestampMs"] < start["serverTimestampMs"]):
            return missing("backend_structure_unavailable")
        paired.append(end)
    if any(starts[i]["startSample"] < paired[i-1]["detectedSample"] for i in range(1, len(starts))):
        return missing("backend_structure_unavailable")
    split = len(starts) > fixture["expected_utterances"]
    for start in starts:
        finals = [e for e in events if e.get("type") == "utterance_finalized"
                  and e.get("sessionId") == session and e.get("utteranceId") == start["utteranceId"]]
        if len(finals) != 1:
            return missing("final_utterance_unavailable", split)
    if any(e.get("type") == "utterance_discarded" and e.get("utteranceId") in ids for e in events):
        return missing("backend_input_discarded", split)
    return {"missing": None, "split": split, "starts": starts, "ends": paired}


def _offset_bounds(row: dict, fixture: dict, start: int, end: int) -> tuple[int, int] | None:
    """独立した二つのanchorと既存v4端部証拠を検証し、既定のsample許容幅で囲む。"""
    if not valid_edges(row, start, end):
        return None
    alignment = row.get("alignment", {})
    size = row.get("input_sample_count")
    if not integer(size):
        return None
    reference_size = (fixture["speech_intervals"][-1]["end_sample"] + fixture["trailing_samples"] + 2) // 3
    if (alignment.get("method") != "two_anchor_normalized_pcm_correlation_v1"
            or alignment.get("status") != "aligned" or alignment.get("reason") is not None
            or alignment.get("sample_rate_hz") != 16000
            or alignment.get("captured_sample_count") != size
            or alignment.get("reference_sample_count") != reference_size
            or alignment.get("alignment_tolerance_samples") != ALIGNMENT_TOLERANCE_SAMPLES):
        return None
    anchors = alignment.get("anchors")
    if not isinstance(anchors, list) or len(anchors) != 2:
        return None
    midpoint, width = (start + end) // 2, min(3200, (end - start) // 3)
    offsets = []
    for anchor, (low, high) in zip(anchors, ((start, midpoint), (midpoint, end)), strict=True):
        ref, cap = anchor.get("reference_start_sample"), anchor.get("captured_start_sample")
        score, competing = anchor.get("correlation"), anchor.get("competing_peak_correlation")
        if (not integer(ref) or not low <= ref <= high - width
                or not integer(cap) or not 0 <= cap <= size - width
                or anchor.get("sample_count") != width or anchor.get("accepted") is not True
                or type(score) not in (int, float) or not math.isfinite(score) or not .8 <= score <= 1
                or type(competing) not in (int, float) or not math.isfinite(competing)
                or not 0 <= competing <= 1 or score - competing < .1):
            return None
        offsets.append(int(cap - ref))
    difference = max(offsets) - min(offsets)
    if difference > MAX_LAG_DIFFERENCE_SAMPLES or alignment.get("lag_difference_samples") != difference:
        return None
    return min(offsets) - ALIGNMENT_TOLERANCE_SAMPLES, max(offsets) + ALIGNMENT_TOLERANCE_SAMPLES


def measure_backend(trial: dict, fixture: dict, ordinal: int,
                    events: list[dict] | None, observed: list[dict] | None) -> dict:
    structure = _structure(trial, fixture)
    split = structure["split"]
    def missing(reason):
        return {"missing": reason, "split": split}
    if events is None or observed is None:
        return missing("backend_media_alignment_unavailable")
    if structure["missing"] is not None:
        return missing(structure["missing"])
    # 誤分割自体は確定して残す。複数HTTP入力を結合して正しい一発話の証拠にはしない。
    if split:
        return missing("backend_split_pcm_alignment_unavailable")
    start, end = structure["starts"][0], structure["ends"][0]
    match = correlate_final_pcm(
        trial, observed, events, session=trial["session_id"], utterance=start["utteranceId"],
        digest=fixture["audio_sha256"], ordinal=ordinal, phase="labeled",
    )
    if match["missing"] is not None:
        return missing("backend_pcm_input_unavailable")
    values = {}
    for key in (
        "stt_capture_media_span_valid", "stt_capture_media_start_sample", "stt_capture_media_end_sample",
        "stt_capture_raw_sample_count", "stt_input_raw_sample_count", "stt_input_prepared_sample_count",
        "stt_input_removed_prefix_samples", "vad_started_sample", "vad_active_end_sample", "vad_detected_end_sample",
    ):
        points = [e.get("value") for e in match["trace"] if e.get("name") == key]
        if len(points) != 1 or not integer(points[0]):
            return missing("backend_media_alignment_unavailable")
        values[key] = int(points[0])
    first, last = values["stt_capture_media_start_sample"], values["stt_capture_media_end_sample"]
    raw, prepared, removed = (values[k] for k in (
        "stt_input_raw_sample_count", "stt_input_prepared_sample_count", "stt_input_removed_prefix_samples"))
    if (values["stt_capture_media_span_valid"] != 1 or not first <= last
            or last - first != raw or raw != values["stt_capture_raw_sample_count"] or raw != prepared + removed
            or values["vad_started_sample"] != start["startSample"]
            or values["vad_active_end_sample"] != end["activeEndSample"]
            or values["vad_detected_end_sample"] != end["detectedSample"] or last != end["detectedSample"]):
        return missing("backend_media_alignment_unavailable")
    source_start = fixture["speech_intervals"][0]["start_sample"] // 3
    source_end = (fixture["speech_intervals"][-1]["end_sample"] + 2) // 3
    try:
        offsets = _offset_bounds(match["row"], fixture, source_start, source_end)
    except (KeyError, TypeError, ValueError):
        offsets = None
    if offsets is None:
        return missing("backend_pcm_alignment_unverified")
    low, high = offsets
    origin = first + removed
    onset = ((start["startSample"] - origin - source_start - high) / 16,
             (start["startSample"] - origin - source_start - low) / 16)
    ending = ((end["detectedSample"] - origin - source_end - high) / 16,
              (end["detectedSample"] - origin - source_end - low) / 16)
    return {
        "onset_offset_lower_ms": onset[0], "onset_offset_upper_ms": onset[1],
        "end_offset_lower_ms": ending[0], "end_offset_upper_ms": ending[1],
        "leading_error": onset[1] > 100, "early_end_error": ending[0] < -100,
        "boundary_uncertain": onset[0] <= 100 < onset[1] or ending[0] < -100 <= ending[1],
        "split": split,
    }

from __future__ import annotations

import math
from collections.abc import Mapping


def validate_playback_summary(raw: dict[str, object], source_samples: Mapping[str, float | None]) -> dict[str, float]:
    """送信PCM量とブラウザー出力時計を照合し、実測したgapを保持する。"""
    counts = {}
    for name in (
        "expected_samples", "input_samples", "padding_samples", "rendered_samples", "packet_count",
        "first_output_frame", "last_output_end_frame", "gap_samples", "maximum_gap_samples", "gap_count",
        "first_rtp_timestamp", "last_rtp_timestamp", "sample_rate",
    ):
        value = raw.get(name)
        if type(value) is not int or not 0 <= value <= 2**53 - 1:
            raise ValueError("invalid completion sample count")
        counts[name] = value
    expected, gap, packets = counts["expected_samples"], counts["gap_samples"], counts["packet_count"]
    if (counts["sample_rate"] != 48000 or not expected or expected != packets * 960
        or counts["rendered_samples"] != expected
        or counts["input_samples"] + counts["padding_samples"] != expected
        or counts["padding_samples"] > 1919
        or counts["last_output_end_frame"] - counts["first_output_frame"] != expected + gap
        or not 0 <= counts["maximum_gap_samples"] <= gap
        or (gap == 0) != (counts["gap_count"] == 0)
        or (gap == 0) != (counts["maximum_gap_samples"] == 0)
        or counts["gap_count"] > gap
        or gap > counts["gap_count"] * counts["maximum_gap_samples"]):
        raise ValueError("completion sample conservation mismatch")
    if (counts["first_rtp_timestamp"] > 0xffffffff or counts["last_rtp_timestamp"] > 0xffffffff
        or (counts["last_rtp_timestamp"] - counts["first_rtp_timestamp"]) % 2**32 != ((packets - 1) * 960) % 2**32):
        raise ValueError("completion RTP timeline mismatch")
    for field, name in (("input_samples", "response_audio_input_samples"),
                        ("expected_samples", "response_audio_captured_samples"),
                        ("padding_samples", "response_audio_padding_samples")):
        if source_samples.get(name) != counts[field]:
            raise ValueError("completion does not match source trace")
    times: list[float] = []
    for name in ("output_clock_context_time", "output_clock_performance_time", "confirmation_observed_at_ms"):
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid completion output clock")
        times.append(value)
    context, performance, observed = times
    end_at = performance + (counts["last_output_end_frame"] / 48000 - context) * 1000
    if context * 48000 < counts["last_output_end_frame"] or not 0 <= end_at <= observed:
        raise ValueError("completion output clock has not confirmed the response")
    return {"playback_gap_total_ms": gap / 48,
            "playback_gap_maximum_ms": counts["maximum_gap_samples"] / 48,
            "playback_underrun_count": float(counts["gap_count"]),
            "playback_duration_ms": (expected + gap) / 48}



def summary_from_playback_observation(raw: dict[str, object]) -> dict[str, object]:
    """旧測定manifestのブラウザー観測を、通信契約のフィールドへ変換する。"""
    return {
        "expected_samples": raw.get("expectedSamples"),
        "input_samples": raw.get("inputSamples"),
        "padding_samples": raw.get("paddingSamples"),
        "rendered_samples": raw.get("renderedSamples"),
        "packet_count": raw.get("packetCount"),
        "first_output_frame": raw.get("firstOutputFrame"),
        "last_output_end_frame": raw.get("lastOutputEndFrame"),
        "gap_samples": raw.get("gapSamples"),
        "maximum_gap_samples": raw.get("maximumGapSamples"),
        "gap_count": raw.get("gapCount"),
        "first_rtp_timestamp": raw.get("firstRtpTimestamp"),
        "last_rtp_timestamp": raw.get("lastRtpTimestamp"),
        "output_clock_context_time": raw.get("outputClockContextTime"),
        "output_clock_performance_time": raw.get("outputClockPerformanceTime"),
        "confirmation_observed_at_ms": raw.get("confirmationObservedAtMs"),
        "sample_rate": raw.get("sampleRate"),
    }

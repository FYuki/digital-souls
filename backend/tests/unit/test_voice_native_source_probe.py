from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts/voice_quality/probe_native_audio_source.py"
)
_SPEC = importlib.util.spec_from_file_location("voice_native_source_probe", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


def observations():
    base = {
        "mode": "direct",
        "errors": [],
        "mainTimeOrigin": 100000,
        "clocks": [
            {"lower": 1000, "workerNow": 10, "upper": 1000.1, "workerOrigin": 99700}
        ]
        * 10,
        "packets": [],
        "decoded": [],
        "rendered": [],
        "completed": [],
        "outputObservation": {
            "observedAtMs": 1050,
            "sampleRate": 48000,
            "outputTimestamp": {"contextTime": 10},
        },
    }
    result = []
    for phase, count in (
        ("before_input", 0),
        ("after_first_20ms", 1),
        ("after_paced_audio", 51),
    ):
        row = deepcopy(base)
        row["phase"] = phase
        for index in range(count):
            row["packets"].append(
                {
                    "index": index,
                    "rtpTimestamp": (4294967200 + index * 960) % 2**32,
                    "sequenceNumber": (65530 + index) % 65536,
                    "rawReceiveTime": 100 + index * 20,
                }
            )
            row["decoded"].append(
                {
                    "packetIndex": index,
                    "chunkTimestamp": index * 20000,
                    "workerNow": 101 + index * 20,
                    "frames": 960,
                    "sampleRate": 48000,
                    "channels": 1,
                }
            )
            row["rendered"].append(
                {
                    "packetIndex": index,
                    "chunkTimestamp": index * 20000,
                    "firstFrame": index * 960,
                    "samples": 960,
                    "outputAtMs": 1110 + index * 20,
                }
            )
            row["completed"].append(
                {
                    "packetIndex": index,
                    "chunkTimestamp": index * 20000,
                    "endFrame": (index + 1) * 960,
                }
            )
        result.append(row)
    return result


def test_causal_clock_survives_wrong_time_origin_and_rtp_wrap():
    rows = observations()
    result = probe.verify_direct_chain(rows)
    assert result["packets"] == 51
    assert result["origin_offset_difference_ms"] < -1200
    assert result["worker_clock_offset_bounds_ms"] == pytest.approx(
        {"lowerMs": 989.8, "upperMs": 990.3}
    )
    assert result["source_pcm_offset_verified"] is False
    assert result["production_playback_verified"] is False


@pytest.mark.parametrize(
    "damage",
    [
        "wrong_packet",
        "lost_packet",
        "incomplete_render",
        "premature_output",
        "early_packet",
        "wide_clock",
        "decoder_error",
    ],
)
def test_rejects_false_media_evidence(damage):
    rows = observations()
    final = rows[-1]
    if damage == "wrong_packet":
        final["decoded"][1]["packetIndex"] = 0
    elif damage == "lost_packet":
        final["packets"][1]["sequenceNumber"] += 1
    elif damage == "incomplete_render":
        final["completed"][1]["endFrame"] -= 1
    elif damage == "premature_output":
        final["outputObservation"]["outputTimestamp"]["contextTime"] = 0
    elif damage == "early_packet":
        final["packets"][0]["rawReceiveTime"] = 0
    elif damage == "wide_clock":
        for clock in final["clocks"]:
            clock["upper"] += 10
    elif damage == "decoder_error":
        final["errors"].append({"kind": "decoder_error"})
    with pytest.raises(ValueError):
        probe.verify_direct_chain(rows)

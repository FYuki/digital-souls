import importlib.util
import json
import sys
from pathlib import Path

import pytest
from app.voice_metrics import TraceEvent

# CLIと同じ隣接scriptのimport範囲で集計器をロードする。
script_dir = Path(__file__).resolve().parents[3] / "scripts/voice_quality"
sys.path.insert(0, str(script_dir))
try:
    spec = importlib.util.spec_from_file_location(
        "backchannel_report", script_dir / "report_backchannel.py"
    )
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
finally:
    sys.path.remove(str(script_dir))


def case(count=100):
    fixtures, trials, events = [], [], []
    for i in range(count):
        session, utterance, response = (
            f"private-session-{i}",
            f"private-utterance-{i}",
            f"private-response-{i}",
        )
        fixture = {
            "cohort": "backchannel",
            "audio_sha256": f"{i:064x}",
            "speech_intervals": [{"start_sample": 4800, "end_sample": 24000}],
        }
        fixtures.append(fixture)
        completion = {
            "expectedSamples": 96000,
            "inputSamples": 95000,
            "paddingSamples": 1000,
            "renderedSamples": 96000,
            "packetCount": 100,
            "firstOutputFrame": 48000,
            "lastOutputEndFrame": 144000,
            "gapSamples": 0,
            "maximumGapSamples": 0,
            "gapCount": 0,
            "firstRtpTimestamp": 1000,
            "lastRtpTimestamp": 96040,
            "sampleRate": 48000,
            "outputClockContextTime": 3,
            "outputClockPerformanceTime": 2500,
            "confirmationObservedAtMs": 2510,
        }
        packet = {
            "packetIndex": 0,
            "outputClockPassed": True,
            "sourcePcmOffsetVerified": False,
            "sampleRate": 48000,
            "receivedAtMs": 350,
            "decodedAtMs": 400,
            "firstOutputAtMs": 500,
            "confirmationObservedAtMs": 510,
            "firstOutputFrame": 48000,
            "firstOutputEndFrame": 48128,
            "outputClockContextTime": 48128 / 48000,
            "outputClockPerformanceTime": 500 + 128 / 48,
        }
        media = {
            "firstPacketDecodedSamples": 960,
            "packetDecodeMissingReason": None,
            "firstPacketReceivedAtMs": 350,
            "firstPacketDecodedAtMs": 400,
        }
        trial = {
            "fixture_sha256": fixture["audio_sha256"],
            "cohort": "backchannel",
            "outcome": "success",
            "session_id": session,
            "old_response_id": response,
            "initial_utterance_id": utterance,
            "injection_playback": {"active": True},
            "session_end_confirmed": True,
            "fixture_clock_bounds": {
                name: {"lowerMs": t, "upperMs": t + 2, "sourceSample": sample}
                for name, t, sample in [
                    ("sourceStart", 1000, 0),
                    ("speechStart", 1100, 4800),
                    ("speechEnd", 1500, 24000),
                ]
            },
            "decision": {
                "sessionId": session,
                "responseId": response,
                "utteranceId": f"private-interruption-{i}",
                "final": True,
                "decision": "backchannel",
            },
            "initial_playback": {
                "response_id": response,
                "track_response_matches": True,
                "track_media_observation": media,
                "packet_playback_observation": packet,
            },
            "evidence": {
                "cycles": [
                    {
                        "sessionId": session,
                        "responseId": response,
                        "utteranceId": utterance,
                        "startedAt": 500,
                    }
                ],
                "transport_failures": [],
                "core_events": [],
                "interruptions": [],
                "playback_completions": {response: completion},
            },
        }
        trials.append(trial)
        for name, value in [
            ("response_audio_input_samples", 95000),
            ("response_audio_captured_samples", 96000),
            ("response_audio_padding_samples", 1000),
        ]:
            events.append(
                TraceEvent(
                    schema_version="1.0",
                    measurement_kind="controlled_baseline",
                    event_id=f"private-event-{i}-{name}",
                    character_id="miori",
                    session_id=session,
                    utterance_id=utterance,
                    response_id=response,
                    name=name,
                    stage="transport",
                    outcome="success",
                    timestamp=1000000000,
                    clock_domain="server_monotonic",
                    unit="nanosecond",
                    value=value,
                )
            )
    return (
        {
            "measurement_scope": "labeled_livekit_interruption_diagnostic",
            "cohort": "backchannel",
            "expected_measured": count,
            "trials": trials,
        },
        events,
        {"trials": fixtures},
    )


def test_full_source_and_output_evidence_can_prove_no_false_cancel():
    result = report.summarize(*case())
    assert result["evaluation"]["false_cancel_rate_passed"] is True
    assert result["metrics"][0]["success_count"] == 100
    assert "private-" not in json.dumps(result)


@pytest.mark.parametrize("cancel_count,passed", [(2, True), (3, False)])
@pytest.mark.parametrize("kind", ["local", "server"])
def test_local_stops_and_server_cancels_count_independently(cancel_count, passed, kind):
    args = case()
    for t in args[0]["trials"][:cancel_count]:
        if kind == "local":
            t["evidence"]["interruptions"] = [
                {"responseId": t["old_response_id"], "localPlaybackStoppedAtMs": 1200}
            ]
        else:
            t["evidence"]["core_events"] = [
                {
                    "type": "response_cancelled",
                    "responseId": t["old_response_id"],
                    "sessionId": t["session_id"],
                }
            ]
    result = report.summarize(*args)
    assert result["metrics"][0]["failure_count"] == cancel_count
    assert result["evaluation"]["false_cancel_rate_passed"] is passed


@pytest.mark.parametrize(
    "invalid",
    [
        "no_completion",
        "no_packet",
        "wrong_response",
        "wrong_source",
        "duplicate_source",
        "no_cycles",
        "transport_failure",
        "invalid_clock",
        "unfinished_fixture",
        "invalid_local_stop",
    ],
)
def test_unverified_continuity_is_missing_not_success(invalid):
    args = case()
    t = args[0]["trials"][0]
    completion = t["evidence"]["playback_completions"][t["old_response_id"]]
    if invalid == "no_completion":
        t["evidence"]["playback_completions"] = {}
    elif invalid == "no_packet":
        t["initial_playback"].pop("packet_playback_observation")
    elif invalid == "wrong_response":
        t["initial_playback"]["response_id"] = "other"
    elif invalid == "wrong_source":
        args[1][0] = args[1][0].model_copy(update={"value": 94000})
    elif invalid == "duplicate_source":
        args[1].append(args[1][0])
    elif invalid == "no_cycles":
        t["evidence"]["cycles"] = []
    elif invalid == "transport_failure":
        t["evidence"]["transport_failures"] = [{"stage": "playback"}]
    elif invalid == "invalid_clock":
        completion["outputClockContextTime"] = 2
    elif invalid == "unfinished_fixture":
        t["fixture_clock_bounds"]["speechEnd"].update(lowerMs=3000, upperMs=3002)
    else:
        t["evidence"]["interruptions"] = [
            {"responseId": t["old_response_id"], "localPlaybackStoppedAtMs": None}
        ]
    result = report.summarize(*args)
    assert result["metrics"][0]["missing_count"] == 1
    assert result["metrics"][0]["rate_denominator"] == 100
    assert result["evaluation"]["false_cancel_rate_passed"] is False


def test_unrecognized_backchannel_still_reports_classification_gap_separately():
    args = case()
    args[0]["trials"][0].pop("decision")
    result = report.summarize(*args)
    assert result["counts"]["backchannel_decision"] == 99
    assert result["metrics"][0]["success_count"] == 100


def test_injection_and_teardown_and_minimum_cohort_are_required():
    args = case(20)
    assert report.summarize(*args)["evaluation"]["false_cancel_rate_passed"] is False
    args = case()
    args[0]["trials"][0]["session_end_confirmed"] = False
    assert report.summarize(*args)["evaluation"]["coverage_complete"] is False
    args[0]["trials"][1]["fixture_clock_bounds"]["sourceStart"]["upperMs"] = 1025
    result = report.summarize(*args)
    assert result["metrics"][0]["missing_outcomes"] == {
        "fixture_injection_unverified": 1
    }


@pytest.mark.parametrize(
    "invalid", ["incomplete", "order", "cohort", "sessions", "mixed_trace"]
)
def test_mixed_or_partial_manifests_are_rejected(invalid):
    args = case()
    if invalid == "incomplete":
        args[0]["trials"].pop()
    elif invalid == "order":
        args[0]["trials"].reverse()
    elif invalid == "cohort":
        args[0]["trials"][0]["cohort"] = "take_turn"
    elif invalid == "sessions":
        args[0]["trials"][1]["session_id"] = args[0]["trials"][0]["session_id"]
    else:
        args[1][0] = args[1][0].model_copy(update={"character_id": "different"})
    with pytest.raises(ValueError):
        report.summarize(*args)

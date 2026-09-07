import copy
import importlib.util
import json
from pathlib import Path

import pytest
from app.voice_metrics import TraceEvent

spec = importlib.util.spec_from_file_location(
    "take_turn_report",
    str(
        Path(__file__).resolve().parents[3]
        / "scripts/voice_quality/report_take_turn.py"
    ),
)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def case(count=100, missed=0):
    trials, fixtures, events = [], [], []
    for i in range(count):
        session, utterance, response = (
            f"private-session-{i}",
            f"private-utterance-{i}",
            f"private-response-{i}",
        )
        fixture = {
            "cohort": "take_turn",
            "audio_sha256": f"{i:064x}",
            "speech_intervals": [{"start_sample": 4800, "end_sample": 24000}],
        }
        fixtures.append(fixture)
        trial = {
            "fixture_sha256": fixture["audio_sha256"],
            "cohort": "take_turn",
            "outcome": "success",
            "session_id": session,
            "old_response_id": response,
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
                "utteranceId": utterance,
                "final": True,
                "decision": "take_turn",
            },
            "evidence": {
                "interruptions": [
                    {
                        "responseId": response,
                        "speechStartedAtMs": 1100,
                        "localPlaybackStoppedAtMs": 1300,
                        "cancelConfirmedAtMs": 1340,
                    }
                ],
                "core_events": [
                    {
                        "type": "response_cancelled",
                        "sessionId": session,
                        "responseId": response,
                    }
                ],
            },
        }
        if i < missed:
            trial.update(outcome="failure", failure_stage="fixture_and_decision")
            trial.pop("decision")
            trial["evidence"] = {}
        else:
            for name, t in [
                ("speech_started_client", 1100),
                ("turn_decision_client", 1300),
                ("local_playback_stopped", 1300),
                ("server_cancelled_client", 1340),
                ("take_turn_decision", 1200000000),
                ("server_cancelled", 1210000000),
            ]:
                server = name in ("take_turn_decision", "server_cancelled")
                events.append(
                    TraceEvent(
                        schema_version="1.0",
                        measurement_kind="controlled_baseline",
                        event_id=f"{i}-{name}",
                        character_id="private-character",
                        session_id=session,
                        utterance_id=utterance,
                        response_id=response,
                        name=name,
                        stage="turn",
                        outcome="success",
                        timestamp=t,
                        clock_domain="server_monotonic"
                        if server
                        else "client_monotonic",
                        unit="nanosecond" if server else "millisecond",
                    )
                )
        trials.append(trial)
    return (
        {
            "measurement_scope": "labeled_livekit_interruption_diagnostic",
            "cohort": "take_turn",
            "expected_measured": count,
            "trials": trials,
        },
        events,
        {"trials": fixtures},
    )


def test_full_real_scope_requires_all_trials_and_keeps_clock_domains_separate():
    result = report.summarize(*case())
    assert result["evaluation"]["missed_rate_passed"]
    assert all(result["evaluation"]["latency_passed"].values())
    assert {m["name"]: m["p95"] for m in result["metrics"]} == {
        "local_playback_stop": 200,
        "turn_decision": 200,
        "cancel_after_decision": 10,
        "barge_in_cancel_total": 240,
    }
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "count,missed,passed", [(100, 1, True), (100, 2, False), (99, 0, False)]
)
def test_failure_denominator_and_one_percent_boundary(count, missed, passed):
    result = report.summarize(*case(count, missed))
    assert result["missed_interruptions"]["count"] == missed
    assert result["missed_interruptions"]["denominator"] == count
    assert result["evaluation"]["missed_rate_passed"] is passed
    assert all(
        m["trial_count"] == count and m["missing_count"] == missed
        for m in result["metrics"]
    )
    if missed:
        assert not any(result["evaluation"]["latency_passed"].values())


def test_partial_run_cannot_be_published_as_complete():
    manifest, events, fixtures = case()
    manifest["trials"].pop()
    with pytest.raises(ValueError, match="all expected"):
        report.summarize(manifest, events, fixtures)


@pytest.mark.parametrize(
    "field,value",
    [
        ("response_id", "another-response"),
        ("utterance_id", "another-utterance"),
        ("session_id", "another-session"),
    ],
)
def test_trace_from_other_response_or_utterance_cannot_fill_missing(field, value):
    manifest, events, fixtures = case(1)
    events = [event.model_copy(update={field: value}) for event in events]
    result = report.summarize(manifest, events, fixtures)
    assert result["counts"]["verified_cancel"] == 0
    assert all(m["missing_count"] == 1 for m in result["metrics"])


@pytest.mark.parametrize(
    "changes",
    [
        {"clock_domain": "server_monotonic"},
        {"unit": "nanosecond"},
        {"timestamp": float("nan")},
    ],
)
def test_wrong_clock_or_nonfinite_timestamp_is_missing(changes):
    manifest, events, fixtures = case(1)
    events[1] = events[1].model_copy(update=changes)
    result = report.summarize(manifest, events, fixtures)
    metric = next(m for m in result["metrics"] if m["name"] == "turn_decision")
    assert metric["missing_count"] == 1 and metric["p95"] is None


def test_duplicate_point_is_not_chosen_arbitrarily():
    manifest, events, fixtures = case(1)
    events.append(events[1].model_copy(update={"event_id": "duplicate"}))
    result = report.summarize(manifest, events, fixtures)
    metric = next(m for m in result["metrics"] if m["name"] == "turn_decision")
    assert metric["missing_outcomes"] == {"trace_point_duplicated": 1}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t["injection_playback"].update(active=False),
        lambda t: t["fixture_clock_bounds"]["speechStart"].update(sourceSample=999),
        lambda t: t["fixture_clock_bounds"]["speechStart"].update(upperMs=1200),
    ],
)
def test_unverified_injection_does_not_reduce_denominator_into_a_pass(mutate):
    manifest, events, fixtures = case()
    mutate(manifest["trials"][0])
    result = report.summarize(manifest, events, fixtures)
    assert result["counts"]["verified_injection"] == 99
    assert result["missed_interruptions"]["unverified_injection_count"] == 1
    assert not result["evaluation"]["coverage_complete"]
    assert not result["evaluation"]["missed_rate_passed"]


def test_observer_and_trace_stop_must_agree():
    manifest, events, fixtures = case(1)
    manifest["trials"][0]["evidence"]["interruptions"][0][
        "localPlaybackStoppedAtMs"
    ] += 10
    result = report.summarize(manifest, events, fixtures)
    assert result["counts"]["verified_cancel"] == 0
    assert (
        next(m for m in result["metrics"] if m["name"] == "local_playback_stop")[
            "missing_count"
        ]
        == 1
    )


def test_nonindependent_sessions_or_changed_fixture_order_are_rejected():
    manifest, events, fixtures = case(2)
    invalid = copy.deepcopy(manifest)
    invalid["trials"][1]["session_id"] = invalid["trials"][0]["session_id"]
    with pytest.raises(ValueError, match="independent"):
        report.summarize(invalid, events, fixtures)
    manifest["trials"].reverse()
    with pytest.raises(ValueError, match="fixture coverage"):
        report.summarize(manifest, events, fixtures)


def test_cleanup_failure_prevents_complete_coverage():
    manifest, events, fixtures = case()
    manifest["trials"][0].update(
        outcome="failure", cleanup_failed=True, session_end_confirmed=False
    )
    result = report.summarize(manifest, events, fixtures)
    assert not result["evaluation"]["coverage_complete"]
    assert result["failure_stages"] == {"cleanup": 1}


def test_local_stop_is_measured_even_if_server_cancel_confirmation_is_missing():
    manifest, events, fixtures = case(1)
    events = [event for event in events if event.name != "server_cancelled_client"]
    manifest["trials"][0]["evidence"]["interruptions"][0]["cancelConfirmedAtMs"] = None
    manifest["trials"][0]["evidence"]["core_events"] = []
    result = report.summarize(manifest, events, fixtures)
    metrics = {m["name"]: m for m in result["metrics"]}
    assert metrics["local_playback_stop"]["p95"] == 200
    assert metrics["turn_decision"]["p95"] == 200
    assert metrics["cancel_after_decision"]["missing_outcomes"] == {
        "server_cancel_not_confirmed": 1
    }
    assert metrics["barge_in_cancel_total"]["missing_count"] == 1
    assert result["counts"]["verified_cancel"] == 0


@pytest.mark.parametrize("difference,valid", [(1, True), (2, True), (3, False)])
def test_separate_client_observers_allow_only_bounded_timestamp_difference(
    difference, valid
):
    manifest, events, fixtures = case(1)
    manifest["trials"][0]["evidence"]["interruptions"][0]["cancelConfirmedAtMs"] += (
        difference
    )
    result = report.summarize(manifest, events, fixtures)
    assert result["counts"]["verified_cancel"] == int(valid)

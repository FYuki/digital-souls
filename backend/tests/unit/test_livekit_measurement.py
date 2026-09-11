from __future__ import annotations

import asyncio

from app.conversation_core import StageObservation
from app.livekit_transport.measurement import LiveKitMeasurementSession
from app.voice_metrics import TraceEvent


def test_livekit_measurement_correlates_buffered_core_stages_with_response() -> None:
    recorded: list[TraceEvent] = []
    timestamps = iter(range(1_000, 2_000))
    measurement = LiveKitMeasurementSession(
        session_id="session-1",
        character_id="miori",
        measurement_kind="dogfood",
        record=recorded.append,
        clock_ns=lambda: next(timestamps),
    )

    async def exercise() -> None:
        await measurement.record(StageObservation(
            session_id="session-1",
            response_id=None,
            generation=None,
            utterance_id="utterance-1",
            stage="stt",
            outcome="started",
        ))
        await measurement.record(StageObservation(
            session_id="session-1",
            response_id=None,
            generation=None,
            utterance_id="utterance-1",
            stage="stt",
            outcome="completed",
        ))
        await measurement.record(StageObservation(
            session_id="session-1",
            response_id="response-1",
            generation=1,
            stage="llm",
            outcome="started",
        ))

    asyncio.run(exercise())
    assert recorded == []

    measurement.bind_response(
        response_id="response-1",
        source_utterance_ids=("utterance-1",),
    )

    assert [event.name for event in recorded] == [
        "stt_started",
        "stt_completed",
        "llm_started",
    ]
    assert {
        (event.session_id, event.utterance_id, event.response_id)
        for event in recorded
    } == {("session-1", "utterance-1", "response-1")}
    assert {event.character_id for event in recorded} == {"miori"}


def test_livekit_measurement_retries_client_observations_after_correlation() -> None:
    recorded: list[TraceEvent] = []
    measurement = LiveKitMeasurementSession(
        session_id="session-1",
        character_id="miori",
        measurement_kind="dogfood",
        record=recorded.append,
        clock_ns=lambda: 1_000,
    )
    speech_stopped = {
        "event_id": "speech-event",
        "session_id": "session-1",
        "utterance_id": "utterance-1",
        "measurement": "speech_stopped",
        "timestamp": 900,
        "clock_domain": "client_monotonic",
        "unit": "millisecond",
    }
    playback_started = {
        "event_id": "playback-event",
        "session_id": "session-1",
        "response_id": "response-1",
        "measurement": "playback_started",
        "timestamp": 1_250,
        "clock_domain": "client_monotonic",
        "unit": "millisecond",
    }

    assert measurement.record_client_observation(speech_stopped) is False
    assert measurement.record_client_observation(speech_stopped) is False
    assert measurement.record_client_observation(playback_started) is False
    measurement.record_utterance_event(
        utterance_id="utterance-1",
        name="vad_speech_end",
        stage="vad",
    )
    measurement.bind_response(
        response_id="response-1",
        source_utterance_ids=("utterance-1",),
    )

    assert [event.name for event in recorded] == [
        "vad_speech_end",
        "speech_stopped",
        "first_playback",
    ]
    assert measurement.record_client_observation(speech_stopped) is False
    assert measurement.record_client_observation(playback_started) is False


def test_livekit_measurement_normalizes_and_deduplicates_client_observations() -> None:
    recorded: list[TraceEvent] = []
    measurement = LiveKitMeasurementSession(
        session_id="session-1",
        character_id="miori",
        measurement_kind="controlled_baseline",
        record=recorded.append,
        clock_ns=lambda: 1_000,
    )
    measurement.bind_response(
        response_id="response-1",
        source_utterance_ids=("utterance-1",),
    )
    playback = {
        "type": "observation",
        "event_id": "event-1",
        "session_id": "session-1",
        "response_id": "response-1",
        "measurement": "playback_started",
        "timestamp": 1_250,
        "clock_domain": "client_monotonic",
        "unit": "millisecond",
    }

    assert measurement.record_client_observation(playback) is True
    assert measurement.record_client_observation(playback) is False
    assert measurement.record_client_observation({
        **playback,
        "event_id": "event-2",
        "session_id": "different-session",
    }) is False

    assert len(recorded) == 1
    assert recorded[0].name == "first_playback"
    assert recorded[0].stage == "playback"
    assert recorded[0].timestamp == 1_250
    assert recorded[0].clock_domain == "client_monotonic"
    assert recorded[0].unit == "millisecond"


def test_livekit_measurement_keeps_client_speech_end_for_ttfa() -> None:
    recorded: list[TraceEvent] = []
    measurement = LiveKitMeasurementSession(
        session_id="session-1",
        character_id="miori",
        measurement_kind="dogfood",
        record=recorded.append,
        clock_ns=lambda: 1_000,
    )
    measurement.record_utterance_event(
        utterance_id="utterance-1",
        name="vad_speech_end",
        stage="vad",
    )

    assert measurement.record_client_observation({
        "type": "observation",
        "event_id": "event-1",
        "session_id": "session-1",
        "utterance_id": "utterance-1",
        "measurement": "speech_stopped",
        "timestamp": 900,
        "clock_domain": "client_monotonic",
        "unit": "millisecond",
    }) is True
    measurement.bind_response(
        response_id="response-1",
        source_utterance_ids=("utterance-1",),
    )

    speech_stopped = next(event for event in recorded if event.name == "speech_stopped")
    assert speech_stopped.timestamp == 900
    assert speech_stopped.clock_domain == "client_monotonic"


def test_livekit_measurement_records_only_first_event_per_trial_point() -> None:
    recorded: list[TraceEvent] = []
    measurement = LiveKitMeasurementSession(
        session_id="session-1",
        character_id="miori",
        measurement_kind="dogfood",
        record=recorded.append,
        clock_ns=lambda: 1_000,
    )
    measurement.bind_response(
        response_id="response-1",
        source_utterance_ids=("utterance-1",),
    )

    measurement.record_utterance_event(
        utterance_id="utterance-1",
        name="user_audio_received",
        stage="transport",
    )
    measurement.record_utterance_event(
        utterance_id="utterance-1",
        name="user_audio_received",
        stage="transport",
    )
    measurement.record_response_event(
        response_id="response-1",
        name="first_text_delta",
        stage="llm",
    )
    measurement.record_response_event(
        response_id="response-1",
        name="first_text_delta",
        stage="llm",
    )

    assert [event.name for event in recorded] == [
        "user_audio_received",
        "first_text_delta",
    ]


def test_livekit_measurement_keeps_stt_failure_without_protocol_response() -> None:
    recorded: list[TraceEvent] = []
    measurement = LiveKitMeasurementSession(
        session_id="session-1",
        character_id="miori",
        measurement_kind="dogfood",
        record=recorded.append,
        clock_ns=lambda: 1_000,
    )

    asyncio.run(measurement.record(StageObservation(
        session_id="session-1",
        response_id=None,
        generation=None,
        utterance_id="utterance-1",
        stage="stt",
        outcome="failed",
    )))

    assert len(recorded) == 1
    assert recorded[0].name == "stt_failed"
    assert recorded[0].outcome == "failure"
    assert recorded[0].reason_code == "stt_failed"
    assert recorded[0].response_id


def test_interruption_keeps_old_response_and_client_clock_in_report(tmp_path) -> None:
    import json
    from pathlib import Path
    from app.livekit_trace_report import finalize_livekit_dogfood_report

    events = []
    measurement = LiveKitMeasurementSession(
        session_id="session-test", character_id="character-test",
        measurement_kind="dogfood", record=events.append, clock_ns=lambda: 900_000_000,
    )
    measurement.bind_response(response_id="old-response", source_utterance_ids=("old-utterance",))
    measurement.record_response_event(
        response_id="old-response", name="first_playback", stage="playback",
        timestamp=500, clock_domain="client_monotonic", unit="millisecond",
    )
    assert measurement.bind_interruption(utterance_id="interruption", response_id="old-response")
    assert not measurement.bind_interruption(utterance_id="interruption", response_id="unknown")
    for name, timestamp, domain, unit in (
        ("speech_started_client", 1000, "client_monotonic", "millisecond"),
        ("speech_started", 2_000_000_000, "server_monotonic", "nanosecond"),
        ("local_playback_stopped", 1150, "client_monotonic", "millisecond"),
        ("take_turn_decision", 2_100_000_000, "server_monotonic", "nanosecond"),
        ("server_cancelled", 2_150_000_000, "server_monotonic", "nanosecond"),
    ):
        measurement.record_utterance_event(
            utterance_id="interruption", name=name, stage="turn",
            timestamp=timestamp, clock_domain=domain, unit=unit,
        )
    for name, timestamp in (("turn_decision_received", 1140), ("cancel_confirmed", 1200)):
        event = {
            "event_id": name, "session_id": "session-test",
            "utterance_id": "interruption", "response_id": "old-response",
            "measurement": name, "timestamp": timestamp,
            "clock_domain": "client_monotonic", "unit": "millisecond",
        }
        assert not measurement.record_client_observation({**event, "response_id": "unknown"})
        # IDを変えた正しい通知だけを受理し、重複も拒否する。
        event["event_id"] = name + "-valid"
        assert measurement.record_client_observation(event)
        assert not measurement.record_client_observation(event)
    measurement.bind_response(response_id="new-response", source_utterance_ids=("interruption",))
    measurement.record_response_event(
        response_id="new-response", name="first_playback", stage="playback",
        timestamp=2000, clock_domain="client_monotonic", unit="millisecond",
    )
    assert not any(e.name == "local_playback_stopped" and e.response_id == "new-response" for e in events)
    trace = tmp_path / "trace.jsonl"
    trace.write_text("".join(e.model_dump_json() + "\n" for e in events))
    output = tmp_path / "report.json"
    finalize_livekit_dogfood_report(
        trace_paths=[trace], output_path=output,
        schema_path=Path(__file__).resolve().parents[3] / "docs/schemas/voice-quality-artifact-v1.schema.json",
        run_id="interruption-test",
    )
    report = json.loads(output.read_text())
    metrics = {m["name"]: m for m in report["metrics"]}
    assert report["run_counts"]["measured"] == 2
    assert report["run_counts"]["failure"] == 0
    for name, duration in {
        "local_playback_stop": 150, "turn_decision": 140,
        "cancel_after_decision": 50, "barge_in_cancel_total": 200,
    }.items():
        assert metrics[name]["p95"] == duration
        assert metrics[name]["rate_denominator"] == 1
        assert metrics[name]["excluded_outcomes"] == {"not_interruption_trial": 2}
    assert metrics["processing_failure"]["rate_denominator"] == 2
    assert metrics["processing_failure"]["excluded_outcomes"] == {"interruption_trial": 1}


def test_client_media_points_keep_distinct_timestamps_and_correlate_once():
    recorded = []
    measurement = LiveKitMeasurementSession(
        session_id='session', character_id='miori', measurement_kind='controlled_baseline',
        record=recorded.append, clock_ns=lambda: 1000000000,
    )
    for name, timestamp in (
        ('client_track_received', 100), ('client_encoded_received', 700),
        ('client_audio_decoded', 710), ('playback_started', 760),
    ):
        event = {'event_id': name, 'session_id': 'session', 'response_id': 'response',
                 'measurement': name, 'timestamp': timestamp,
                 'clock_domain': 'client_monotonic', 'unit': 'millisecond'}
        assert not measurement.record_client_observation(event)
    measurement.bind_response(response_id='response', source_utterance_ids=('utterance',))
    assert [(event.name, event.timestamp) for event in recorded] == [
        ('client_track_received', 100), ('client_audio_received', 700),
        ('client_audio_decoded', 710), ('first_playback', 760),
    ]
    assert all(event.response_id == 'response' and event.utterance_id == 'utterance' for event in recorded)

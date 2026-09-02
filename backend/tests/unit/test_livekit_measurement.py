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

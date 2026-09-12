"""無応答、操作失敗、記録欠落を0件へ誤変換しないことを確認する。"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.voice_metrics import JsonlTraceRecorder
from app.voice_session_metrics import SessionMetrics, SessionSummary, aggregate_sessions
from app.livekit_transport.coordinator import ProductionSessionCoordinator, SessionCoordinatorDependencies, APPLICATION_TOPIC
from app.voice_session.validation import parse_voice_session_event


def summary(**changes):
    return dict(sequence=1, microphone_activation_attempts=1, mute_attempts=0,
                retry_attempts=0, operation_tracking_started=True, end_requested=False, **changes)


def setup():
    events = []
    metrics = SessionMetrics(character_id="fixture", session_id=str(uuid4()),
                             measurement_kind="controlled_baseline", record=events.append)
    return metrics, events


def report(events, expected=1):
    return aggregate_sessions(events, measurement_kind="controlled_baseline", expected_sessions=expected)


def test_zero_response_session_has_native_end_and_operations():
    metrics, events = setup()
    metrics.activate()
    metrics.observe_summary({**summary(), "end_requested": True})
    metrics.end("explicit")
    result = report(events)
    assert result["counts"]["zero_completed_response_sessions"] == 1
    assert result["counts"]["expected_ends"] == 1
    assert result["additional_operations"]["values"] == [0]
    assert result["three_turn_additional_operations"]["measured_sessions"] == 0
    assert result["missing_sessions_by_reason"] == {}
    raw = json.dumps(result)
    assert events[0].session_id not in raw and "fixture" not in raw
    assert all(event.response_id is None for event in events)


def test_three_validated_playbacks_and_failed_manual_attempts_deduplicate():
    metrics, events = setup()
    metrics.activate()
    first = summary()
    metrics.observe_summary(first)
    metrics.observe_summary(first)
    for response in ("one", "two", "two", "three"):
        metrics.completed_playback(response)
    metrics.observe_summary({**first, "sequence": 3, "microphone_activation_attempts": 3,
                             "mute_attempts": 1, "retry_attempts": 2, "end_requested": True})
    metrics.end("explicit")
    metrics.end("runtime_error")
    result = report(events + events)
    assert result["three_turn_additional_operations"] == {"measured_sessions": 1, "values": [5]}
    assert len([event for event in events if event.name == "ended"]) == 1


@pytest.mark.parametrize("reason", ["reconnect_timeout", "runtime_error", "protocol_error"])
def test_unexpected_end_without_response_is_in_denominator(reason):
    metrics, events = setup()
    metrics.activate()
    metrics.end(reason)
    result = report(events)
    assert result["counts"]["unexpected_ends"] == 1
    assert result["counts"]["zero_completed_response_sessions"] == 1
    assert result["additional_operations"]["values"] == []
    assert result["missing_sessions_by_reason"]["complete_operation_window_not_recorded"] == 1


def test_server_explicit_shutdown_is_not_assumed_user_end():
    metrics, events = setup()
    metrics.activate()
    metrics.end("explicit")
    assert report(events)["missing_sessions_by_reason"]["end_intent_not_confirmed"] == 1


@pytest.mark.parametrize("change", [dict(sequence=0), dict(mute_attempts=-1),
                                     dict(microphone_activation_attempts=0), dict(sequence=True),
                                     dict(end_requested="true"), dict(secret="forbidden")])
def test_invalid_or_reversed_summary_is_missing(change):
    metrics, events = setup()
    metrics.activate()
    metrics.observe_summary(summary())
    metrics.observe_summary({**summary(), "sequence": 2, **change})
    metrics.end("explicit")
    assert report(events)["missing_sessions_by_reason"] == {"incomplete_or_invalid_journal": 1}


def test_missing_native_events_and_absent_sessions_are_not_zero():
    metrics, events = setup()
    metrics.activate()
    metrics.end("reconnect_timeout")
    assert report(events[1:])["missing_sessions_by_reason"]["incomplete_or_invalid_journal"] == 1
    assert report([], 2)["missing_sessions_by_reason"] == {"session_journal_absent": 2}
    assert report(events[:-1])["missing_sessions_by_reason"]["session_end_not_recorded_or_cleanup_failed"] == 1


def test_cleanup_failure_is_not_successful_session_end():
    metrics, events = setup()
    metrics.activate()
    metrics.end("explicit", cleanup_completed=False)
    assert "ended_sessions" not in report(events)["counts"]


def test_bootstrap_session_stays_separate_from_active_failure_denominator():
    metrics, events = setup()
    metrics.end("join_token_expired")
    assert report(events)["counts"] == dict(created_sessions=1, never_activated_sessions=1, ended_sessions=1)


def test_recording_error_cannot_break_voice_and_gap_remains_visible():
    metrics, events = setup()
    def fail(_):
        raise OSError("private path must not be logged")
    metrics._record = fail
    metrics.activate()
    metrics._record = events.append
    metrics.end("runtime_error")
    assert report(events)["missing_sessions_by_reason"]["incomplete_or_invalid_journal"] == 1


def test_sidecar_does_not_pollute_response_jsonl(tmp_path: Path):
    path = tmp_path / "run.jsonl"
    recorder = JsonlTraceRecorder(path)
    SessionMetrics(character_id="fixture", session_id="real-session", measurement_kind="dogfood",
                   record=recorder.record_session)
    assert not path.exists()
    assert json.loads((tmp_path / "sessions" / "run.jsonl").read_text())["name"] == "created"


def event(session_id, **changes):
    return dict(type="observation", protocol_version="1.1", event_id=str(uuid4()),
                session_id=session_id, measurement="session_summary", timestamp=100,
                clock_domain="client_monotonic", unit="millisecond", session_summary=summary(), **changes)


def test_contract_accepts_session_summary_without_fake_utterance_and_rejects_foreign_fields():
    raw = event(str(uuid4()))
    parse_voice_session_event(raw)
    for field in ("utterance_id", "response_id", "audio", "transcript"):
        with pytest.raises(ValueError):
            parse_voice_session_event({**raw, field: str(uuid4())})
    for field, value in (("sequence", True), ("mute_attempts", -1), ("secret", "never")):
        with pytest.raises(ValueError):
            parse_voice_session_event({**raw, "session_summary": {**summary(), field: value}})
    with pytest.raises(ValidationError):
        SessionSummary.model_validate({**summary(), "retry_attempts": 1.5})


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_actual_coordinator_records_activation_summary_and_native_cleanup(cleanup_fails):
    async def exercise():
        metrics, events = setup()
        cleanup = AsyncMock(side_effect=RuntimeError() if cleanup_fails else None)
        class Port:
            def notify(self, payload):
                pass
        coordinator = ProductionSessionCoordinator(
            session_id=events[0].session_id, user_identity="user", core_participant_id="core",
            reconnect_grace_ms=60000, core_port=Port(),
            dependencies=SessionCoordinatorDependencies(publish_data=AsyncMock(), cleanup=cleanup,
                generation_ready=AsyncMock(), session_metrics=metrics))
        coordinator.participant_connected(identity="user", participant_sid="sid", room_sid="room")
        coordinator.participant_connected(identity="user", participant_sid="sid", room_sid="room")
        raw = event(events[0].session_id)
        raw["session_summary"]["end_requested"] = True
        for _ in range(2):
            await coordinator.receive_data(identity="user", participant_sid="sid", topic=APPLICATION_TOPIC,
                                           payload=json.dumps(raw).encode())
        if cleanup_fails:
            with pytest.raises(RuntimeError):
                await coordinator.cleanup("explicit")
        else:
            await coordinator.cleanup("explicit")
            assert report(events)["counts"]["expected_ends"] == 1
        assert [e.name for e in events] == ["created", "activated", "summary",
                                            "cleanup_failed" if cleanup_fails else "ended"]
    asyncio.run(exercise())


def test_old_identical_retry_after_new_snapshot_does_not_invalidate_journal():
    metrics, events = setup()
    metrics.activate()
    first = summary()
    metrics.observe_summary(first)
    metrics.observe_summary({**first, "sequence": 2, "end_requested": True})
    metrics.observe_summary(first)
    metrics.end("explicit")
    assert report(events)["missing_sessions_by_reason"] == {}


def test_contradictory_lifecycle_cannot_pass_reaggregation():
    metrics, events = setup()
    metrics.activate()
    metrics.end("explicit")
    # 有効schemaの連番でも開始を2度記録したjournalは不正。
    events[1] = events[1].model_copy(update={"name": "created"})
    assert report(events)["missing_sessions_by_reason"] == {"incomplete_or_invalid_journal": 1}


def test_committed_schemas_match_runtime_models():
    from app.voice_session_metrics import SessionAggregate, SessionTraceEvent
    schema_root = Path(__file__).resolve().parents[3] / "docs" / "schemas"
    for model, name in [(SessionAggregate, "voice-session-aggregate-v1.schema.json"),
                        (SessionTraceEvent, "voice-session-trace-v1.schema.json")]:
        assert json.loads((schema_root / name).read_text()) == model.model_json_schema()

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _voice_metrics():
    return pytest.importorskip(
        "app.voice_metrics",
        reason="音声計測moduleの実装後に有効化する契約テストです",
    )


def _event(
    metrics,
    *,
    event_id: str,
    name: str,
    timestamp: int | float,
    clock_domain: str = "server_monotonic",
    stage: str = "stt",
    outcome: str = "success",
    reason_code: str | None = None,
    unit: str = "nanosecond",
    value: float | None = None,
):
    return metrics.TraceEvent(
        schema_version="1.0",
        measurement_kind="automated_test",
        event_id=event_id,
        session_id="session-1",
        utterance_id="utterance-1",
        response_id="response-1",
        name=name,
        stage=stage,
        outcome=outcome,
        reason_code=reason_code,
        timestamp=timestamp,
        clock_domain=clock_domain,
        unit=unit,
        value=value,
    )


def _aggregation_context(metrics, *, kind="automated_test", measured_runs=1):
    unavailable = metrics.DiagnosticValue(
        status="not_applicable",
        reason="test_diagnostic_not_supplied",
    )
    metadata = metrics.create_run_metadata(
        measurement_kind=kind,
        transport="websocket",
        run_id="test-run",
        profile="automated-test",
        fixture_version="speech-v2",
        warmup_runs=0,
        measured_runs=measured_runs,
        whisper_model="medium",
        whisper_device="cpu",
        whisper_compute_type="int8",
    )
    diagnostics = metrics.RunDiagnostics(
        clocks=[],
        hardware=metrics.HardwareMetadata(description="test hardware"),
        resources=metrics.ResourceMetadata(
            cpu_percent=unavailable,
            memory_bytes=unavailable,
        ),
        network=metrics.NetworkMetadata(
            sent_bytes=unavailable,
            received_bytes=unavailable,
            packet_loss_basis_points=unavailable,
            condition="test",
        ),
    )
    return metadata, diagnostics


def test_vm_id_01_serialized_event_keeps_the_complete_correlation_chain() -> None:
    metrics = _voice_metrics()
    event = _event(
        metrics,
        event_id="event-1",
        name="stt_started",
        timestamp=1_000_000,
    )

    serialized = metrics.serialize_trace_event(event, {})

    assert serialized["event_id"] == "event-1"
    assert serialized["session_id"] == "session-1"
    assert serialized["utterance_id"] == "utterance-1"
    assert serialized["response_id"] == "response-1"


def test_vm_priv_01_trace_serialization_is_an_allow_list() -> None:
    metrics = _voice_metrics()
    event = _event(
        metrics,
        event_id="event-private",
        name="stt_completed",
        timestamp=2_000_000,
    )
    sentinels = {
        "prompt": "SECRET_PROMPT_5C73",
        "transcript": "SECRET_TRANSCRIPT_D981",
        "audio_body": "SECRET_AUDIO_BODY_A12E",
        "audio_payload": "SECRET_AUDIO_PAYLOAD_7B44",
        "exception": "SECRET_EXCEPTION_E271",
    }

    serialized = metrics.serialize_trace_event(event, sentinels)

    serialized_values = {
        value
        for value in serialized.values()
        if isinstance(value, str)
    }
    assert serialized_values.isdisjoint(sentinels.values())
    assert set(serialized).isdisjoint(sentinels)


def test_vm_clock_01_duration_uses_only_one_clock_domain() -> None:
    metrics = _voice_metrics()
    started = _event(
        metrics,
        event_id="event-start",
        name="stt_started",
        timestamp=1_000_000,
    )
    completed = _event(
        metrics,
        event_id="event-end",
        name="stt_completed",
        timestamp=3_500_000,
    )

    duration = metrics.duration_ms(started, completed)

    assert duration == 2.5


def test_vm_clock_01_rejects_subtraction_between_clock_domains() -> None:
    metrics = _voice_metrics()
    fixture_end = _event(
        metrics,
        event_id="fixture-end",
        name="fixture_speech_ended",
        timestamp=1_000_000,
        clock_domain="fixture_timeline",
    )
    playback = _event(
        metrics,
        event_id="playback",
        name="first_playback",
        timestamp=3_500_000,
        clock_domain="client_monotonic",
    )

    with pytest.raises(ValueError):
        metrics.duration_ms(fixture_end, playback)


@pytest.mark.parametrize(
    ("quantile", "expected"),
    [(0.50, 2.5), (0.95, 3.85)],
)
def test_vm_stat_01_uses_hyndman_fan_type_7_quantiles(
    quantile: float,
    expected: float,
) -> None:
    metrics = _voice_metrics()

    actual = metrics.type7_quantile([1.0, 2.0, 3.0, 4.0], quantile)

    assert actual == pytest.approx(expected)


def test_vm_outcome_01_keeps_status_counts_and_rate_denominator_distinct() -> None:
    metrics = _voice_metrics()
    observations = [
        metrics.MetricObservation.measured(100.0),
        metrics.MetricObservation.measured(200.0),
        metrics.MetricObservation.failed("tts_failed"),
        metrics.MetricObservation.missing("playback_event_missing"),
        metrics.MetricObservation.not_applicable("websocket_has_no_cancel"),
        metrics.MetricObservation.excluded("privacy_skip"),
    ]

    aggregate = metrics.aggregate_metric("ttfa", observations)

    assert aggregate.trial_count == 6
    assert aggregate.success_count == 2
    assert aggregate.failure_count == 1
    assert aggregate.missing_count == 1
    assert aggregate.not_applicable_count == 1
    assert aggregate.excluded_count == 1
    assert aggregate.rate_denominator == 4
    assert aggregate.failure_rate_basis_points == 2_500


def test_vm_outcome_01_missing_event_is_not_reported_as_not_applicable() -> None:
    metrics = _voice_metrics()

    aggregate = metrics.aggregate_metric(
        "client_playback_latency",
        [metrics.MetricObservation.missing("first_playback_event_missing")],
    )

    assert aggregate.status == "missing"
    assert aggregate.missing_count == 1
    assert aggregate.not_applicable_count == 0
    assert aggregate.not_applicable_reason is None


def test_vm_outcome_01_static_websocket_gap_has_a_not_applicable_reason() -> None:
    metrics = _voice_metrics()

    aggregate = metrics.aggregate_metric(
        "cancel_after_decision",
        [metrics.MetricObservation.not_applicable("websocket_cancel_not_implemented")],
    )

    assert aggregate.status == "not_applicable"
    assert aggregate.not_applicable_count == 1
    assert aggregate.missing_count == 0
    assert aggregate.not_applicable_reason == "websocket_cancel_not_implemented"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("not_applicable", None),
        ("not_applicable", ""),
        ("measured", "websocket_cancel_not_implemented"),
        ("missing", "first_playback_event_missing"),
    ],
)
def test_vm_outcome_01_model_rejects_invalid_status_reason_pairs(
    status: str,
    reason: str | None,
) -> None:
    metrics = _voice_metrics()
    valid = metrics.aggregate_metric(
        "cancel_after_decision",
        [metrics.MetricObservation.not_applicable("websocket_cancel_not_implemented")],
    ).model_dump()
    valid.update(status=status, not_applicable_reason=reason)

    with pytest.raises(ValueError):
        metrics.MetricAggregate.model_validate(valid)


def test_vm_outcome_01_projects_one_primary_failure_consistently() -> None:
    metrics = _voice_metrics()
    outcomes = [
        metrics.StageOutcome.failed("tts", "tts_upstream_failed"),
        metrics.StageOutcome.failed("transport", "socket_closed"),
    ]

    result = metrics.summarize_response_outcomes(outcomes)

    assert result.status == "failed"
    assert result.stage == "tts"
    assert result.reason_code == "tts_upstream_failed"
    assert result.abort_reason == "tts_upstream_failed"


def test_vm_outcome_01_exclusions_do_not_become_processing_failures() -> None:
    metrics = _voice_metrics()

    for reason in ("normal_cancel", "privacy_skip", "user_ended", "fault_injected"):
        result = metrics.summarize_response_outcomes(
            [metrics.StageOutcome.excluded(reason)]
        )
        assert result.status == "excluded"
        assert result.reason_code == reason
        assert result.failure_count == 0


def test_vm_kind_01_rejects_mixed_measurement_kinds() -> None:
    metrics = _voice_metrics()
    automated = _event(
        metrics,
        event_id="automated",
        name="stt_started",
        timestamp=1,
    )
    controlled = automated.model_copy(
        update={"event_id": "controlled", "measurement_kind": "controlled_baseline"}
    )

    with pytest.raises(ValueError):
        metadata, diagnostics = _aggregation_context(
            metrics,
            kind="controlled_baseline",
        )
        metrics.aggregate_events(
            [automated, controlled],
            metadata=metadata,
            diagnostics=diagnostics,
        )


def test_vm_art_01_aggregate_drops_all_reverse_lookup_ids() -> None:
    metrics = _voice_metrics()
    events = [
        _event(
            metrics,
            event_id="start",
            name="stt_started",
            timestamp=1_000_000,
        ),
        _event(
            metrics,
            event_id="end",
            name="stt_completed",
            timestamp=2_000_000,
        ),
    ]

    metadata, diagnostics = _aggregation_context(metrics)
    artifact = metrics.aggregate_events(
        events,
        metadata=metadata,
        diagnostics=diagnostics,
    )
    serialized = artifact.model_dump(mode="json")

    forbidden_keys = {"event_id", "session_id", "utterance_id", "response_id"}
    pending: list[object] = [serialized]
    observed_keys: set[str] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            observed_keys.update(value)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    assert observed_keys.isdisjoint(forbidden_keys)


def test_vm_art_01_aggregates_catalog_once_per_trial() -> None:
    metrics = _voice_metrics()
    first = [
        _event(metrics, event_id="first-start", name="stt_started", timestamp=1_000_000),
        _event(metrics, event_id="first-end", name="stt_completed", timestamp=3_000_000),
    ]
    second = [event.model_copy(update={
        "event_id": f"second-{event.event_id}",
        "session_id": "session-2",
        "utterance_id": "utterance-2",
        "response_id": "response-2",
        "timestamp": event.timestamp + 10_000_000,
    }) for event in first]
    metadata, diagnostics = _aggregation_context(metrics, measured_runs=2)

    artifact = metrics.aggregate_events(
        [*first, *second],
        metadata=metadata,
        diagnostics=diagnostics,
    )

    catalog = {metric.name: metric for metric in artifact.metrics}
    assert artifact.run_counts.measured == 2
    assert artifact.run_counts.success == 2
    assert catalog["stt_processing"].trial_count == 2
    assert catalog["stt_processing"].p50 == 2.0
    assert catalog["client_playback_latency"].status == "missing"
    assert catalog["cancel_after_decision"].status == "not_applicable"


def test_vm_art_01_uses_metric_and_stage_specific_outcomes() -> None:
    metrics = _voice_metrics()
    server_events = [
        ("user_audio_received", 1_000_000, "transport"),
        ("stt_started", 2_000_000, "stt"),
        ("stt_completed", 3_000_000, "stt"),
        ("llm_started", 4_000_000, "llm"),
        ("first_text_delta", 5_000_000, "llm"),
        ("llm_completed", 6_000_000, "llm"),
        ("tts_started", 7_000_000, "tts"),
        ("tts_completed", 8_000_000, "tts"),
    ]
    client_events = [
        ("fixture_speech_start", 100.0, "fixture", None),
        ("captured_audio_start", 90.0, "capture", None),
        ("fixture_speech_end", 200.0, "fixture", None),
        ("vad_speech_end", 205.0, "capture", None),
        ("utterance_finalized", 225.0, "capture", None),
        ("response_decision", 230.0, "response", None),
        ("client_audio_received", 300.0, "transport", None),
        ("first_playback", 350.0, "playback", None),
        ("manual_operations", 231.0, "interaction", 0.0),
    ]
    events = [
        _event(metrics, event_id=name, name=name, timestamp=timestamp, stage=stage)
        for name, timestamp, stage in server_events
    ] + [
        _event(
            metrics,
            event_id=name,
            name=name,
            timestamp=timestamp,
            stage=stage,
            clock_domain="client_monotonic",
            unit="millisecond",
            value=value,
        )
        for name, timestamp, stage, value in client_events
    ]
    metadata, diagnostics = _aggregation_context(metrics)

    artifact = metrics.aggregate_events(events, metadata=metadata, diagnostics=diagnostics)

    catalog = {metric.name: metric for metric in artifact.metrics}
    for name in (
        "utterance_finalized",
        "response_decision",
        "stt_start_latency",
        "vad_leading_boundary",
        "vad_trailing_boundary",
        "manual_operations",
    ):
        assert catalog[name].status == "measured"
    assert catalog["vad_leading_boundary"].p50 == -10.0
    assert catalog["manual_operations"].p50 == 0.0
    assert sum(metric.status == "not_applicable" for metric in artifact.metrics) == 7
    assert {outcome.stage for outcome in artifact.stage_outcomes} == {
        "stt", "llm", "tts", "transport", "playback"
    }


def test_vm_art_01_livekit_speech_stopped_is_ttfa_start() -> None:
    metrics = _voice_metrics()
    events = [
        _event(
            metrics,
            event_id="speech-stopped",
            name="speech_stopped",
            timestamp=1_000.0,
            stage="vad",
            clock_domain="client_monotonic",
            unit="millisecond",
        ),
        _event(
            metrics,
            event_id="playback",
            name="first_playback",
            timestamp=1_750.0,
            stage="playback",
            clock_domain="client_monotonic",
            unit="millisecond",
        ),
    ]
    metadata, diagnostics = _aggregation_context(metrics)
    metadata = metadata.model_copy(update={"transport": "livekit"})

    artifact = metrics.aggregate_events(
        events,
        metadata=metadata,
        diagnostics=diagnostics,
    )

    ttfa = next(metric for metric in artifact.metrics if metric.name == "ttfa")
    assert ttfa.status == "measured"
    assert ttfa.p50 == 750.0
    catalog = {metric.name: metric for metric in artifact.metrics}
    assert catalog["local_playback_stop"].status == "missing"
    assert catalog["reconnect"].status == "missing"
    assert catalog["playback_continuity"].status == "missing"


def test_vm_art_01_does_not_project_a_tts_failure_into_stt_metrics() -> None:
    metrics = _voice_metrics()
    events = [
        _event(metrics, event_id="stt-start", name="stt_started", timestamp=1_000_000),
        _event(metrics, event_id="stt-end", name="stt_completed", timestamp=2_000_000),
        _event(
            metrics,
            event_id="tts-failure",
            name="tts_failed",
            timestamp=3_000_000,
            stage="tts",
            outcome="failure",
            reason_code="tts_upstream_failed",
        ),
    ]
    metadata, diagnostics = _aggregation_context(metrics)

    artifact = metrics.aggregate_events(events, metadata=metadata, diagnostics=diagnostics)

    catalog = {metric.name: metric for metric in artifact.metrics}
    assert catalog["stt_processing"].status == "measured"
    assert catalog["first_audio_generation"].status == "failed"
    assert next(
        outcome for outcome in artifact.stage_outcomes if outcome.stage == "stt"
    ).status == "success"
    assert next(
        outcome for outcome in artifact.stage_outcomes if outcome.stage == "tts"
    ).status == "failed"


def test_vm_outcome_01_privacy_skip_is_excluded_from_failure_denominator() -> None:
    metrics = _voice_metrics()
    event = _event(
        metrics,
        event_id="privacy-skip",
        name="response_excluded",
        timestamp=1_000_000,
        outcome="excluded",
        reason_code="privacy_skip",
    )
    metadata, diagnostics = _aggregation_context(metrics)

    artifact = metrics.aggregate_events(
        [event],
        metadata=metadata,
        diagnostics=diagnostics,
    )

    assert artifact.run_counts.excluded == 1
    assert artifact.run_counts.failure == 0
    processing = next(
        metric for metric in artifact.metrics if metric.name == "processing_failure"
    )
    stt_processing = next(
        metric for metric in artifact.metrics if metric.name == "stt_processing"
    )
    assert processing.excluded_outcomes == {"privacy_skip": 1}
    assert processing.rate_denominator == 0
    assert processing.failure_count == 0
    assert stt_processing.excluded_outcomes == {}


def test_vm_art_01_run_metadata_includes_current_whisper_configuration() -> None:
    metrics = _voice_metrics()

    metadata = metrics.create_run_metadata(
        measurement_kind="controlled_baseline",
        transport="websocket",
        run_id="run-1",
        profile="integration-voice",
        fixture_version="speech-v2",
        warmup_runs=5,
        measured_runs=100,
        whisper_model="medium",
        whisper_device="cpu",
        whisper_compute_type="int8",
    )

    assert metadata.whisper.model == "medium"
    assert metadata.whisper.device == "cpu"
    assert metadata.whisper.compute_type == "int8"


def test_vm_dog_01_removes_only_raw_traces_older_than_seven_days(
    tmp_path: Path,
) -> None:
    metrics = _voice_metrics()
    raw_root = tmp_path / "voice-metrics" / "raw"
    raw_root.mkdir(parents=True)
    now = datetime(2026, 8, 25, tzinfo=timezone.utc)
    expired = raw_root / "expired.jsonl"
    boundary = raw_root / "boundary.jsonl"
    current = raw_root / "current.jsonl"
    for path in (expired, boundary, current):
        path.write_text("{}\n", encoding="utf-8")
    os.utime(
        expired,
        (now.timestamp(), (now - timedelta(days=7, seconds=1)).timestamp()),
    )
    os.utime(boundary, (now.timestamp(), (now - timedelta(days=7)).timestamp()))
    os.utime(current, (now.timestamp(), (now - timedelta(days=1)).timestamp()))

    removed = metrics.cleanup_expired_raw_traces(
        raw_root,
        now=now,
        retention=timedelta(days=7),
    )

    assert removed == [expired]
    assert not expired.exists()
    assert boundary.exists()
    assert current.exists()


def test_vm_dog_01_rejects_raw_trace_storage_inside_repository(
    tmp_path: Path,
) -> None:
    metrics = _voice_metrics()
    repository_root = tmp_path / "repository"
    repository_root.mkdir()

    with pytest.raises(ValueError):
        metrics.resolve_raw_trace_root(
            repository_root=repository_root,
            data_root=repository_root / "data",
            measurement_kind="dogfood",
        )


def test_vm_dog_01_repository_raw_trace_path_is_ignored_by_git() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "voice-metrics/raw/example.jsonl"],
        check=False,
    )

    assert result.returncode == 0


def test_vm_base_01_controlled_run_requires_canonical_trial_shape() -> None:
    metrics = _voice_metrics()

    canonical = metrics.validate_controlled_run(
        warmup_runs=5,
        measured_runs=100,
        session_ids=[f"session-{index}" for index in range(100)],
        conversation_ids=[f"conversation-{index}" for index in range(100)],
        fixture_versions=["speech-v2"] * 105,
        initial_state_hashes=["initial-state-v1"] * 105,
    )

    assert canonical.warmup_runs == 5
    assert canonical.measured_runs == 100
    assert canonical.independent_sessions is True
    assert canonical.independent_conversations is True


@pytest.mark.parametrize(
    ("session_ids", "conversation_ids"),
    [
        (["same"] * 100, [f"conversation-{index}" for index in range(100)]),
        ([f"session-{index}" for index in range(100)], ["same"] * 100),
    ],
)
def test_vm_base_01_rejects_reused_trial_identity(
    session_ids: list[str],
    conversation_ids: list[str],
) -> None:
    metrics = _voice_metrics()

    with pytest.raises(ValueError):
        metrics.validate_controlled_run(
            warmup_runs=5,
            measured_runs=100,
            session_ids=session_ids,
            conversation_ids=conversation_ids,
            fixture_versions=["speech-v2"] * 105,
            initial_state_hashes=["initial-state-v1"] * 105,
        )


def test_vm_base_01_fixture_boundary_is_used_for_ttfa() -> None:
    metrics = _voice_metrics()

    ttfa_ms = metrics.fixture_ttfa_ms(
        fixture_speech_end_sample=16_000,
        sample_rate_hz=16_000,
        fixture_started_client_ms=4_000.0,
        first_playback_client_ms=5_750.0,
    )

    assert ttfa_ms == 750.0


def test_vm_art_01_schema_accepts_anonymous_controlled_aggregate() -> None:
    metrics = _voice_metrics()
    schema_path = Path("docs/schemas/voice-quality-artifact-v1.schema.json")
    assert schema_path.is_file(), "音声品質artifact schemaが未実装です"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    artifact = metrics.AggregateArtifact.model_validate(
        {
            "schema_version": "1.0",
            "measurement_kind": "controlled_baseline",
            "transport": "websocket",
            "run_id": "controlled-run",
            "profile": "integration-voice",
            "fixture_version": "speech-v2",
            "quantile_method": "hyndman_fan_type_7",
            "clocks": [
                {
                    "domain": "client_monotonic",
                    "method": "performance_now",
                    "unit": "millisecond",
                }
            ],
            "run_counts": {
                "warmup": 5,
                "measured": 100,
                "success": 100,
                "failure": 0,
                "excluded": 0,
            },
            "metrics": [],
            "stage_outcomes": [],
            "whisper": {
                "model": "medium",
                "device": "cpu",
                "compute_type": "int8",
            },
            "hardware": {"description": "test hardware"},
            "resources": {
                "cpu_percent": {"status": "measured", "value": 10.0},
                "memory_bytes": {"status": "measured", "value": 1024},
            },
            "network": {
                "sent_bytes": {"status": "measured", "value": 100},
                "received_bytes": {"status": "measured", "value": 200},
                "packet_loss_basis_points": {"status": "measured", "value": 0},
                "condition": "local",
            },
        }
    ).model_dump(mode="json")

    from jsonschema import validate

    validate(instance=artifact, schema=schema)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("not_applicable", None),
        ("measured", "websocket_cancel_not_implemented"),
    ],
)
def test_vm_art_01_schema_rejects_invalid_status_reason_pairs(
    status: str,
    reason: str | None,
) -> None:
    from jsonschema import ValidationError, validate

    schema = json.loads(
        Path("docs/schemas/voice-quality-artifact-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    metric = _voice_metrics().aggregate_metric(
        "cancel_after_decision",
        [_voice_metrics().MetricObservation.not_applicable("websocket_cancel_not_implemented")],
    ).model_dump(mode="json")
    metric.update(status=status, not_applicable_reason=reason)
    artifact = {
        "schema_version": "1.0", "measurement_kind": "controlled_baseline",
        "transport": "websocket", "run_id": "run", "profile": "integration-voice",
        "fixture_version": "speech-v2", "quantile_method": "hyndman_fan_type_7",
        "clocks": [], "run_counts": {"warmup": 5, "measured": 100, "success": 100, "failure": 0, "excluded": 0},
        "metrics": [metric], "stage_outcomes": [],
        "whisper": {"model": "medium", "device": "cpu", "compute_type": "int8"},
        "hardware": {"description": "test"},
        "resources": {"cpu_percent": {"status": "measured", "value": 1, "reason": None}, "memory_bytes": {"status": "measured", "value": 1, "reason": None}},
        "network": {"sent_bytes": {"status": "measured", "value": 1, "reason": None}, "received_bytes": {"status": "measured", "value": 1, "reason": None}, "packet_loss_basis_points": {"status": "measured", "value": 0, "reason": None}, "condition": "local"},
    }

    with pytest.raises(ValidationError):
        validate(instance=artifact, schema=schema)


def _write_valid_baseline_inputs(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    manifest_path = tmp_path / "manifest.json"
    trace_path = tmp_path / "trace.jsonl"
    trials = []
    trace_lines = []
    for index in range(105):
        measured_index = index - 5
        session_id = f"session-{measured_index}" if index >= 5 else f"warmup-session-{index}"
        utterance_id = f"utterance-{index}"
        response_id = f"response-{index}"
        trials.append({
            "phase": "measured" if index >= 5 else "warmup",
            "fixture_version": "speech-v2",
            "audio_sha256": "a" * 64,
            "transcript_matches": True,
            "initial_state_hash": "initial-state-v1",
            "fixture_speech_end_client_ms": 1_000.0,
            "fixtureStartedAt": 0.0,
            "sendAt": 1_100.0,
            "audioReceivedAt": 1_500.0,
            "audioDecodeAt": 1_510.0,
            "startedAt": 1_750.0,
            "latencyMs": 650.0,
            "sessionId": session_id,
            "utteranceId": utterance_id,
            "responseId": response_id,
            "conversationId": f"conversation-{measured_index}" if index >= 5 else f"warmup-conversation-{index}",
            "sentBytes": 32000,
            "receivedBytes": 64000,
        })
        trace_lines.append(json.dumps({
            "schema_version": "1.0",
            "measurement_kind": "controlled_baseline",
            "event_id": f"event-{index}",
            "session_id": session_id,
            "utterance_id": utterance_id,
            "response_id": response_id,
            "name": "first_playback",
            "stage": "playback",
            "outcome": "success",
            "reason_code": None,
            "timestamp": 1750.0,
            "clock_domain": "client_monotonic",
            "unit": "millisecond",
        }))
    manifest: dict[str, object] = {
        "fixture": {
            "fixture_version": "speech-v2",
            "audio_sha256": "a" * 64,
            "sample_rate_hz": 1_000,
            "speech_start_sample": 250,
            "speech_end_sample": 1_000,
        },
        "initial_state_hash": "initial-state-v1",
        "trials": trials,
        "diagnostics": {"cpu_percent": 12.5, "maximum_resident_set_bytes": 1024},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    trace_path.write_text("\n".join(trace_lines) + "\n", encoding="utf-8")
    return manifest_path, trace_path, manifest


def test_vm_base_01_finalizer_excludes_warmups_and_writes_anonymous_artifact(
    tmp_path: Path,
) -> None:
    from app.voice_baseline import finalize_baseline

    manifest_path, trace_path, _manifest = _write_valid_baseline_inputs(tmp_path)
    output_path = tmp_path / "artifact.json"

    finalize_baseline(
        manifest_path=manifest_path,
        trace_path=trace_path,
        output_path=output_path,
        schema_path=Path("docs/schemas/voice-quality-artifact-v1.schema.json"),
    )

    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    assert output_path.with_suffix(".md").is_file()
    assert artifact["run_counts"] == {
        "warmup": 5,
        "measured": 100,
        "success": 100,
        "failure": 0,
        "excluded": 0,
    }
    assert next(metric for metric in artifact["metrics"] if metric["name"] == "ttfa")["p95"] == 750.0
    serialized = json.dumps(artifact)
    assert "session_id" not in serialized
    assert "utterance_id" not in serialized
    assert "response_id" not in serialized


@pytest.mark.parametrize(
    "mutate_manifest",
    [
        lambda manifest: manifest["trials"][0].update(fixture_version="speech-v0"),
        lambda manifest: manifest["trials"][0].update(audio_sha256="b" * 64),
        lambda manifest: manifest["trials"][0].update(transcript_matches=False),
    ],
)
def test_vm_base_01_finalizer_rejects_fixture_or_transcript_mismatch(
    tmp_path: Path,
    mutate_manifest,
) -> None:
    from app.voice_baseline import finalize_baseline

    manifest_path, trace_path, manifest = _write_valid_baseline_inputs(tmp_path)
    mutate_manifest(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError):
        finalize_baseline(
            manifest_path=manifest_path,
            trace_path=trace_path,
            output_path=tmp_path / "artifact.json",
            schema_path=Path("docs/schemas/voice-quality-artifact-v1.schema.json"),
        )

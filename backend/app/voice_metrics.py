from __future__ import annotations

import math
import json
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Literal["1.0"] = "1.0"
QUANTILE_METHOD: Literal["hyndman_fan_type_7"] = "hyndman_fan_type_7"
RAW_TRACE_RETENTION = timedelta(days=7)

MeasurementKind = Literal["automated_test", "controlled_baseline", "dogfood"]
Transport = Literal["websocket", "livekit"]
MetricStatus = Literal[
    "measured", "not_applicable", "missing", "failed", "excluded"
]
EventOutcome = Literal["success", "failure", "excluded"]


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    measurement_kind: MeasurementKind
    event_id: str = Field(min_length=1)
    character_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    utterance_id: str = Field(min_length=1)
    response_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    outcome: EventOutcome
    reason_code: str | None = None
    timestamp: int | float = Field(ge=0)
    clock_domain: str = Field(min_length=1)
    unit: Literal["nanosecond", "millisecond"]
    value: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_reason(self) -> TraceEvent:
        if self.outcome == "success" and self.reason_code is not None:
            raise ValueError("successful event must not have a reason code")
        if self.outcome != "success" and self.reason_code is None:
            raise ValueError("non-success event requires a reason code")
        return self


class MeasurementContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    measurement_kind: MeasurementKind
    character_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    utterance_id: str = Field(min_length=1)
    response_id: str = Field(min_length=1)
    record: Callable[[TraceEvent], None]
    clock_ns: Callable[[], int]


TRACE_EVENT_FIELDS = frozenset(TraceEvent.model_fields)


class JsonlTraceRecorder:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: TraceEvent) -> None:
        line = json.dumps(
            serialize_trace_event(event, {}),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            with self._path.open("a", encoding="utf-8") as trace_file:
                trace_file.write(line + "\n")


def serialize_trace_event(
    event: TraceEvent,
    untrusted_payload: object,
) -> dict[str, object]:
    del untrusted_payload
    serialized = event.model_dump(mode="json")
    return {key: serialized[key] for key in TRACE_EVENT_FIELDS}


def duration_ms(started: TraceEvent, completed: TraceEvent) -> float:
    if started.clock_domain != completed.clock_domain:
        raise ValueError("events from different clock domains cannot be subtracted")
    if started.unit != completed.unit:
        raise ValueError("events with different units cannot be subtracted")
    duration = completed.timestamp - started.timestamp
    if duration < 0:
        raise ValueError("completed event must not precede started event")
    if started.unit == "nanosecond":
        return duration / 1_000_000
    return float(duration)


def type7_quantile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("quantile requires at least one value")
    if quantile < 0 or quantile > 1:
        raise ValueError("quantile must be between zero and one")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


class MetricObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MetricStatus
    value: float | None = None
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> MetricObservation:
        if self.status == "measured":
            if self.value is None or self.reason_code is not None:
                raise ValueError("measured observation requires only a value")
        elif self.value is not None or self.reason_code is None:
            raise ValueError("non-measured observation requires only a reason code")
        return self

    @classmethod
    def measured(cls, value: float) -> MetricObservation:
        return cls(status="measured", value=value)

    @classmethod
    def failed(cls, reason_code: str) -> MetricObservation:
        return cls(status="failed", reason_code=reason_code)

    @classmethod
    def missing(cls, reason_code: str) -> MetricObservation:
        return cls(status="missing", reason_code=reason_code)

    @classmethod
    def not_applicable(cls, reason_code: str) -> MetricObservation:
        return cls(status="not_applicable", reason_code=reason_code)

    @classmethod
    def excluded(cls, reason_code: str) -> MetricObservation:
        return cls(status="excluded", reason_code=reason_code)


class MetricAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    status: MetricStatus
    unit: str
    start_point: str
    end_point: str
    trial_count: int
    success_count: int
    failure_count: int
    missing_count: int
    not_applicable_count: int
    excluded_count: int
    rate_denominator: int
    failure_rate_basis_points: int | None
    p50: float | None
    p95: float | None
    not_applicable_reason: str | None
    excluded_outcomes: dict[str, int]

    @model_validator(mode="after")
    def validate_not_applicable_reason(self) -> MetricAggregate:
        has_reason = bool(self.not_applicable_reason)
        if (self.status == "not_applicable") != has_reason:
            raise ValueError(
                "not-applicable status and reason must be present together"
            )
        return self


def _aggregate_status(counts: Counter[MetricStatus]) -> MetricStatus:
    if counts["failed"]:
        return "failed"
    if counts["missing"]:
        return "missing"
    if counts["measured"]:
        return "measured"
    if counts["not_applicable"]:
        return "not_applicable"
    return "excluded"


def aggregate_metric(
    name: str,
    observations: Sequence[MetricObservation],
    *,
    unit: str = "millisecond",
    start_point: str = "unspecified",
    end_point: str = "unspecified",
) -> MetricAggregate:
    if not observations:
        raise ValueError("metric aggregation requires at least one observation")
    counts = Counter(observation.status for observation in observations)
    values = [
        observation.value
        for observation in observations
        if observation.status == "measured" and observation.value is not None
    ]
    denominator = counts["measured"] + counts["failed"] + counts["missing"]
    failure_rate = (
        round(counts["failed"] * 10_000 / denominator) if denominator else None
    )
    status = _aggregate_status(counts)
    not_applicable_reasons = {
        observation.reason_code
        for observation in observations
        if status == "not_applicable" and observation.status == "not_applicable"
    }
    not_applicable_reason = None
    if len(not_applicable_reasons) == 1:
        not_applicable_reason = next(iter(not_applicable_reasons))
    elif len(not_applicable_reasons) > 1:
        raise ValueError("one metric cannot have multiple not-applicable reasons")
    excluded = Counter(
        observation.reason_code
        for observation in observations
        if observation.status == "excluded" and observation.reason_code is not None
    )
    return MetricAggregate(
        name=name,
        status=status,
        unit=unit,
        start_point=start_point,
        end_point=end_point,
        trial_count=len(observations),
        success_count=counts["measured"],
        failure_count=counts["failed"],
        missing_count=counts["missing"],
        not_applicable_count=counts["not_applicable"],
        excluded_count=counts["excluded"],
        rate_denominator=denominator,
        failure_rate_basis_points=failure_rate,
        p50=type7_quantile(values, 0.5) if values else None,
        p95=type7_quantile(values, 0.95) if values else None,
        not_applicable_reason=not_applicable_reason,
        excluded_outcomes=dict(excluded),
    )


class StageOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["success", "failed", "excluded"]
    stage: str | None = None
    reason_code: str | None = None

    @classmethod
    def succeeded(cls, stage: str) -> StageOutcome:
        return cls(status="success", stage=stage)

    @classmethod
    def failed(cls, stage: str, reason_code: str) -> StageOutcome:
        return cls(status="failed", stage=stage, reason_code=reason_code)

    @classmethod
    def excluded(cls, reason_code: str) -> StageOutcome:
        return cls(status="excluded", reason_code=reason_code)


class ResponseOutcomeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["success", "failed", "excluded"]
    stage: str | None
    reason_code: str | None
    abort_reason: str | None
    failure_count: int


def summarize_response_outcomes(
    outcomes: Sequence[StageOutcome],
) -> ResponseOutcomeSummary:
    if not outcomes:
        raise ValueError("response outcome summary requires at least one outcome")
    primary = next(
        (outcome for outcome in outcomes if outcome.status == "failed"),
        next(
            (outcome for outcome in outcomes if outcome.status == "excluded"),
            outcomes[0],
        ),
    )
    return ResponseOutcomeSummary(
        status=primary.status,
        stage=primary.stage,
        reason_code=primary.reason_code,
        abort_reason=primary.reason_code if primary.status == "failed" else None,
        failure_count=sum(outcome.status == "failed" for outcome in outcomes),
    )


class WhisperMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    device: str
    compute_type: str


class ClockMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str
    method: str
    unit: str


class RunCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warmup: int = Field(ge=0)
    measured: int = Field(ge=0)
    success: int = Field(ge=0)
    failure: int = Field(ge=0)
    excluded: int = Field(ge=0)


class DiagnosticValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["measured", "not_applicable", "missing"]
    value: float | int | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> DiagnosticValue:
        if self.status == "measured":
            if self.value is None or self.reason is not None:
                raise ValueError("measured diagnostic requires only a value")
        elif self.value is not None or self.reason is None:
            raise ValueError("unmeasured diagnostic requires only a reason")
        return self


class ResourceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_percent: DiagnosticValue
    memory_bytes: DiagnosticValue


class NetworkMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sent_bytes: DiagnosticValue
    received_bytes: DiagnosticValue
    packet_loss_basis_points: DiagnosticValue
    condition: str


class HardwareMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str


class AggregateArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    measurement_kind: MeasurementKind
    transport: Transport
    run_id: str
    profile: str
    fixture_version: str
    quantile_method: Literal["hyndman_fan_type_7"]
    clocks: list[ClockMetadata]
    run_counts: RunCounts
    metrics: list[MetricAggregate]
    stage_outcomes: list[StageOutcome]
    whisper: WhisperMetadata
    hardware: HardwareMetadata
    resources: ResourceMetadata
    network: NetworkMetadata


class RunMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    measurement_kind: MeasurementKind
    transport: Transport
    run_id: str
    profile: str
    fixture_version: str
    warmup_runs: int
    measured_runs: int
    whisper: WhisperMetadata


class RunDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    clocks: list[ClockMetadata]
    hardware: HardwareMetadata
    resources: ResourceMetadata
    network: NetworkMetadata


def create_run_metadata(
    *,
    measurement_kind: MeasurementKind,
    transport: Transport,
    run_id: str,
    profile: str,
    fixture_version: str,
    warmup_runs: int,
    measured_runs: int,
    whisper_model: str,
    whisper_device: str,
    whisper_compute_type: str,
) -> RunMetadata:
    return RunMetadata(
        schema_version=SCHEMA_VERSION,
        measurement_kind=measurement_kind,
        transport=transport,
        run_id=run_id,
        profile=profile,
        fixture_version=fixture_version,
        warmup_runs=warmup_runs,
        measured_runs=measured_runs,
        whisper=WhisperMetadata(
            model=whisper_model,
            device=whisper_device,
            compute_type=whisper_compute_type,
        ),
    )


@dataclass(frozen=True)
class _MetricDefinition:
    name: str
    start_event: str | None
    end_event: str | None
    start_point: str
    end_point: str
    unit: str = "millisecond"
    not_applicable_reason: str | None = None
    failure_stages: tuple[str, ...] = ()
    exclude_on_response_outcome: bool = False
    value_event: str | None = None
    signed_offset: bool = False


_METRIC_CATALOG = (
    _MetricDefinition("utterance_finalized", "vad_speech_end", "utterance_finalized", "vad_speech_end", "utterance_finalized"),
    _MetricDefinition("response_decision", "vad_speech_end", "response_decision", "vad_speech_end", "response_decision"),
    _MetricDefinition("stt_start_latency", "user_audio_received", "stt_started", "user_audio_received", "stt_started", failure_stages=("stt",)),
    _MetricDefinition("stt_processing", "stt_started", "stt_completed", "stt_started", "stt_completed", failure_stages=("stt",)),
    _MetricDefinition("llm_start_latency", "stt_completed", "llm_started", "stt_completed", "llm_started", failure_stages=("stt", "llm")),
    _MetricDefinition("first_text_latency", "stt_completed", "first_text_delta", "stt_completed", "first_text_delta", failure_stages=("stt", "llm")),
    _MetricDefinition("llm_completion", "llm_started", "llm_completed", "llm_started", "llm_completed", failure_stages=("llm",)),
    _MetricDefinition("tts_start_latency", "first_text_delta", "tts_started", "first_synthesizable_segment", "tts_started", failure_stages=("llm", "tts"), exclude_on_response_outcome=True),
    _MetricDefinition("first_audio_generation", "first_text_delta", "tts_completed", "first_synthesizable_segment", "first_audio_generated", failure_stages=("llm", "tts"), exclude_on_response_outcome=True),
    _MetricDefinition("client_playback_latency", "client_audio_received", "first_playback", "client_audio_received", "first_playback", failure_stages=("transport", "playback"), exclude_on_response_outcome=True),
    _MetricDefinition("ttfa", "fixture_speech_end", "first_playback", "fixture_speech_end", "first_playback", failure_stages=("stt", "llm", "tts", "transport", "playback"), exclude_on_response_outcome=True),
    _MetricDefinition("local_playback_stop", "speech_started", "local_playback_stopped", "speech_started", "local_playback_stopped", not_applicable_reason="websocket_barge_in_not_implemented"),
    _MetricDefinition("turn_decision", "speech_started", "turn_decision", "speech_started", "turn_decision", not_applicable_reason="websocket_turn_decision_not_implemented"),
    _MetricDefinition("cancel_after_decision", "take_turn_decision", "server_cancelled", "take_turn_decision", "server_cancelled", not_applicable_reason="websocket_cancel_not_implemented"),
    _MetricDefinition("barge_in_cancel_total", "speech_started", "server_cancelled", "speech_started", "server_cancelled", not_applicable_reason="websocket_barge_in_not_implemented"),
    _MetricDefinition("vad_leading_boundary", "fixture_speech_start", "captured_audio_start", "fixture_speech_start", "captured_audio_start", signed_offset=True),
    _MetricDefinition("vad_trailing_boundary", "fixture_speech_end", "utterance_finalized", "fixture_speech_end", "utterance_finalized", signed_offset=True),
    _MetricDefinition("stale_output", None, None, "server_cancelled", "stale_output", unit="count", not_applicable_reason="websocket_cancel_not_implemented", value_event="stale_output"),
    _MetricDefinition("reconnect", "network_recovered", "transport_available", "network_recovered", "transport_available", not_applicable_reason="websocket_reconnect_not_instrumented"),
    _MetricDefinition("playback_continuity", "scheduled_playout", "frame_playout", "scheduled_playout", "frame_playout", not_applicable_reason="websocket_playout_not_instrumented"),
    _MetricDefinition("processing_failure", "stt_started", "first_playback", "response_utterance", "processing_outcome", unit="count", failure_stages=("stt", "llm", "tts", "transport", "playback"), exclude_on_response_outcome=True),
    _MetricDefinition("manual_operations", None, None, "session_started", "session_ended", unit="count", value_event="manual_operations"),
)


def _trial_key(event: TraceEvent) -> tuple[str, str, str]:
    return event.session_id, event.utterance_id, event.response_id


def _primary_trial_event(events: Sequence[TraceEvent]) -> TraceEvent:
    return next(
        (event for event in events if event.outcome == "failure"),
        next((event for event in events if event.outcome == "excluded"), events[0]),
    )


def _metric_observation(
    definition: _MetricDefinition,
    trial_events: Sequence[TraceEvent],
    *,
    transport: Transport,
) -> MetricObservation:
    if definition.not_applicable_reason is not None and transport == "websocket":
        return MetricObservation.not_applicable(definition.not_applicable_reason)
    by_name = {event.name: event for event in trial_events}
    # controlled WebSocket runnerはfixture境界を使う。LiveKit TTFAだけは
    # VoiceSessionのspeech_stoppedを同じclient clockの開始点にする。
    # VAD境界指標へ流用するとserver clockとの誤減算になるためaliasしない。
    if (
        definition.name == "ttfa"
        and "fixture_speech_end" not in by_name
        and "speech_stopped" in by_name
    ):
        by_name["fixture_speech_end"] = by_name["speech_stopped"]
    response_exclusion = next(
        (
            event for event in trial_events
            if event.name == "response_excluded" and event.outcome == "excluded"
        ),
        None,
    )
    if definition.exclude_on_response_outcome and response_exclusion is not None:
        assert response_exclusion.reason_code is not None
        return MetricObservation.excluded(response_exclusion.reason_code)
    relevant_failure = next(
        (
            event for event in trial_events
            if event.outcome == "failure" and event.stage in definition.failure_stages
        ),
        None,
    )
    if relevant_failure is not None:
        assert relevant_failure.reason_code is not None
        return MetricObservation.failed(relevant_failure.reason_code)
    if definition.name == "ttfa" and "fixture_speech_end" not in by_name:
        return MetricObservation.not_applicable(
            "fixture_boundary_requires_controlled_runner"
        )
    if definition.name == "processing_failure":
        if "first_playback" not in by_name:
            return MetricObservation.missing("first_playback_event_missing")
        return MetricObservation.measured(0)
    if definition.value_event is not None:
        value_events = [
            event for event in trial_events if event.name == definition.value_event
        ]
        if not value_events:
            return MetricObservation.missing(f"{definition.value_event}_event_missing")
        latest = max(value_events, key=lambda event: event.timestamp)
        if latest.value is None:
            return MetricObservation.missing(f"{definition.value_event}_value_missing")
        return MetricObservation.measured(latest.value)
    assert definition.start_event is not None and definition.end_event is not None
    started = by_name.get(definition.start_event)
    completed = by_name.get(definition.end_event)
    if started is None or completed is None:
        missing = definition.start_event if started is None else definition.end_event
        return MetricObservation.missing(f"{missing}_event_missing")
    if definition.signed_offset:
        if started.clock_domain != completed.clock_domain or started.unit != completed.unit:
            raise ValueError("events from different clock domains cannot be subtracted")
        offset = completed.timestamp - started.timestamp
        if started.unit == "nanosecond":
            offset /= 1_000_000
        return MetricObservation.measured(float(offset))
    return MetricObservation.measured(duration_ms(started, completed))


def _stage_outcomes(trial_events: Sequence[TraceEvent]) -> list[StageOutcome]:
    outcomes: list[StageOutcome] = []
    for stage in ("stt", "llm", "tts", "transport", "playback"):
        stage_events = [event for event in trial_events if event.stage == stage]
        if not stage_events:
            continue
        primary = _primary_trial_event(stage_events)
        outcomes.append(StageOutcome(
            status="failed" if primary.outcome == "failure" else primary.outcome,
            stage=stage,
            reason_code=primary.reason_code,
        ))
    response_exclusion = next(
        (
            event for event in trial_events
            if event.name == "response_excluded" and event.outcome == "excluded"
        ),
        None,
    )
    if response_exclusion is not None:
        outcomes.append(StageOutcome(
            status="excluded",
            stage="response",
            reason_code=response_exclusion.reason_code,
        ))
    return outcomes


def aggregate_events(
    events: Sequence[TraceEvent],
    *,
    metadata: RunMetadata,
    diagnostics: RunDiagnostics,
) -> AggregateArtifact:
    if not events:
        raise ValueError("event aggregation requires at least one event")
    if any(event.measurement_kind != metadata.measurement_kind for event in events):
        raise ValueError("measurement kinds must not be mixed")
    if len({event.character_id for event in events}) != 1:
        raise ValueError("character ids must not be mixed")
    trials: dict[tuple[str, str, str], list[TraceEvent]] = {}
    for event in events:
        trials.setdefault(_trial_key(event), []).append(event)
    primary_events = [_primary_trial_event(trial) for trial in trials.values()]
    outcomes = [
        outcome
        for trial in trials.values()
        for outcome in _stage_outcomes(trial)
    ]
    failures = sum(event.outcome == "failure" for event in primary_events)
    exclusions = sum(event.outcome == "excluded" for event in primary_events)
    successes = len(primary_events) - failures - exclusions
    metric_aggregates = [
        aggregate_metric(
            definition.name,
            [
                _metric_observation(
                    definition,
                    trial,
                    transport=metadata.transport,
                )
                for trial in trials.values()
            ],
            unit=definition.unit,
            start_point=definition.start_point,
            end_point=definition.end_point,
        )
        for definition in _METRIC_CATALOG
    ]
    return AggregateArtifact(
        schema_version=SCHEMA_VERSION,
        measurement_kind=metadata.measurement_kind,
        transport=metadata.transport,
        run_id=metadata.run_id,
        profile=metadata.profile,
        fixture_version=metadata.fixture_version,
        quantile_method=QUANTILE_METHOD,
        clocks=diagnostics.clocks,
        run_counts=RunCounts(
            warmup=metadata.warmup_runs,
            measured=len(trials),
            success=successes,
            failure=failures,
            excluded=exclusions,
        ),
        metrics=metric_aggregates,
        stage_outcomes=outcomes,
        whisper=metadata.whisper,
        hardware=diagnostics.hardware,
        resources=diagnostics.resources,
        network=diagnostics.network,
    )


def cleanup_expired_raw_traces(
    raw_root: Path,
    *,
    now: datetime,
    retention: timedelta = RAW_TRACE_RETENTION,
) -> list[Path]:
    cutoff = now.timestamp() - retention.total_seconds()
    removed: list[Path] = []
    for path in sorted(raw_root.glob("*.jsonl")):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def resolve_raw_trace_root(
    *,
    repository_root: Path,
    data_root: Path,
    measurement_kind: MeasurementKind,
) -> Path:
    repository = repository_root.resolve()
    resolved_data_root = data_root.resolve()
    if measurement_kind == "dogfood" and (
        resolved_data_root == repository
        or repository in resolved_data_root.parents
    ):
        raise ValueError("dogfood raw traces must be stored outside the repository")
    return resolved_data_root / "voice-metrics" / "raw"


class ControlledRunValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warmup_runs: Literal[5]
    measured_runs: Literal[100]
    independent_sessions: Literal[True]
    independent_conversations: Literal[True]


def validate_controlled_run(
    *,
    warmup_runs: int,
    measured_runs: int,
    session_ids: Sequence[str],
    conversation_ids: Sequence[str],
    fixture_versions: Sequence[str],
    initial_state_hashes: Sequence[str],
) -> ControlledRunValidation:
    if warmup_runs != 5 or measured_runs != 100:
        raise ValueError("controlled baseline requires five warmups and 100 trials")
    if len(session_ids) != measured_runs or len(set(session_ids)) != measured_runs:
        raise ValueError("each measured trial requires an independent session")
    if (
        len(conversation_ids) != measured_runs
        or len(set(conversation_ids)) != measured_runs
    ):
        raise ValueError("each measured trial requires an independent conversation")
    all_runs = warmup_runs + measured_runs
    if len(fixture_versions) != all_runs or len(set(fixture_versions)) != 1:
        raise ValueError("all runs require one fixed fixture version")
    if len(initial_state_hashes) != all_runs or len(set(initial_state_hashes)) != 1:
        raise ValueError("all runs require one fixed initial state")
    return ControlledRunValidation(
        warmup_runs=5,
        measured_runs=100,
        independent_sessions=True,
        independent_conversations=True,
    )


def fixture_ttfa_ms(
    *,
    fixture_speech_end_sample: int,
    sample_rate_hz: int,
    fixture_started_client_ms: float,
    first_playback_client_ms: float,
) -> float:
    if sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive")
    fixture_end_client_ms = (
        fixture_started_client_ms
        + fixture_speech_end_sample * 1_000 / sample_rate_hz
    )
    duration = first_playback_client_ms - fixture_end_client_ms
    if duration < 0:
        raise ValueError("first playback must not precede fixture speech end")
    return duration


class TargetEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    absolute_passed: bool | None = None
    relative_passed: bool | None = None
    median_guide_met: bool | None = None
    limit_ms: float | None = None


ABSOLUTE_LATENCY_LIMITS_MS = {
    "ttfa": 2_000.0,
    "local_playback_stop": 150.0,
    "turn_decision": 300.0,
    "cancel_after_decision": 200.0,
    "barge_in_cancel_total": 500.0,
    "utterance_finalized": 800.0,
}


def evaluate_relative_latency(
    livekit_p95_ms: float,
    websocket_p95_ms: float,
) -> TargetEvaluation:
    if livekit_p95_ms < 0 or websocket_p95_ms < 0:
        raise ValueError("latency must not be negative")
    limit = websocket_p95_ms + max(websocket_p95_ms * 0.1, 50.0)
    passed = livekit_p95_ms <= limit
    return TargetEvaluation(passed=passed, relative_passed=passed, limit_ms=limit)


def evaluate_latency_target(
    *,
    metric_name: str,
    p50_ms: float,
    p95_ms: float,
    websocket_p95_ms: float | None,
) -> TargetEvaluation:
    if p50_ms < 0 or p95_ms < 0:
        raise ValueError("latency must not be negative")
    try:
        absolute_limit = ABSOLUTE_LATENCY_LIMITS_MS[metric_name]
    except KeyError as exc:
        raise ValueError(f"latency metric has no absolute target: {metric_name}") from exc
    absolute_passed = p95_ms <= absolute_limit
    relative_passed = (
        None
        if websocket_p95_ms is None
        else evaluate_relative_latency(p95_ms, websocket_p95_ms).passed
    )
    return TargetEvaluation(
        passed=absolute_passed
        and (relative_passed is None or relative_passed),
        absolute_passed=absolute_passed,
        relative_passed=relative_passed,
        median_guide_met=p50_ms <= 1_000 if metric_name == "ttfa" else None,
        limit_ms=absolute_limit,
    )


def _basis_points(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("rate denominator must be positive")
    if numerator < 0 or numerator > denominator:
        raise ValueError("rate numerator must be within the denominator")
    return round(numerator * 10_000 / denominator)


class ReconnectEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    within_ten_seconds_rate_basis_points: int
    recovery_p95_ms: float


def evaluate_reconnect(
    *,
    trials: int,
    recovered_within_ten_seconds: int,
    successful_recovery_ms: Sequence[float],
    duplicate_playbacks: int,
) -> ReconnectEvaluation:
    rate = _basis_points(recovered_within_ten_seconds, trials)
    recovery_p95 = type7_quantile(successful_recovery_ms, 0.95)
    return ReconnectEvaluation(
        passed=(
            trials == 100
            and rate >= 9_900
            and recovery_p95 <= 3_000
            and duplicate_playbacks == 0
        ),
        within_ten_seconds_rate_basis_points=rate,
        recovery_p95_ms=recovery_p95,
    )


class VadEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    leading_loss_rate_basis_points: int
    early_end_rate_basis_points: int
    split_rate_basis_points: int


def evaluate_vad(
    *,
    trials: int,
    leading_losses_over_100_ms: int,
    early_ends_over_100_ms: int,
    utterance_finalize_p95_ms: float,
    intentional_pause_ms: int,
    splits_at_intentional_pause: int,
) -> VadEvaluation:
    leading_rate = _basis_points(leading_losses_over_100_ms, trials)
    early_rate = _basis_points(early_ends_over_100_ms, trials)
    split_rate = _basis_points(splits_at_intentional_pause, trials)
    return VadEvaluation(
        passed=(
            leading_rate <= 100
            and early_rate <= 100
            and utterance_finalize_p95_ms <= 800
            and intentional_pause_ms <= 600
            and split_rate <= 100
        ),
        leading_loss_rate_basis_points=leading_rate,
        early_end_rate_basis_points=early_rate,
        split_rate_basis_points=split_rate,
    )


class TurnClassificationEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    false_cancel_rate_basis_points: int
    missed_interruption_rate_basis_points: int
    indeterminate_count: int
    false_cancel_denominator: int
    missed_interruption_denominator: int


def evaluate_turn_classification(
    *,
    backchannel_trials: int,
    interruption_trials: int,
    false_cancels: int,
    missed_interruptions: int,
    indeterminate: int,
) -> TurnClassificationEvaluation:
    false_rate = _basis_points(false_cancels, backchannel_trials)
    missed_rate = _basis_points(missed_interruptions, interruption_trials)
    return TurnClassificationEvaluation(
        passed=(
            backchannel_trials >= 100
            and interruption_trials >= 100
            and false_rate <= 200
            and missed_rate <= 100
        ),
        false_cancel_rate_basis_points=false_rate,
        missed_interruption_rate_basis_points=missed_rate,
        indeterminate_count=indeterminate,
        false_cancel_denominator=backchannel_trials,
        missed_interruption_denominator=interruption_trials,
    )


class QualityTargetEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    continuity_passed: bool
    processing_passed: bool
    presentation_passed: bool


def evaluate_quality_targets(
    *,
    controlled_underruns: int,
    controlled_processing_failures: int,
    dogfood_gap_ms: int,
    dogfood_playback_ms: int,
    maximum_continuous_gap_ms: int,
    dogfood_processing_failures: int,
    dogfood_response_utterances: int,
    unexpected_session_ends: int,
    stale_presented: int,
    duplicate_playbacks: int,
    required_manual_operations: int,
) -> QualityTargetEvaluation:
    gap_rate = _basis_points(dogfood_gap_ms, dogfood_playback_ms)
    failure_rate = _basis_points(
        dogfood_processing_failures,
        dogfood_response_utterances,
    )
    return QualityTargetEvaluation(
        continuity_passed=(
            controlled_underruns == 0
            and gap_rate <= 10
            and maximum_continuous_gap_ms <= 200
        ),
        processing_passed=(
            controlled_processing_failures == 0
            and failure_rate <= 100
            and unexpected_session_ends == 0
        ),
        presentation_passed=(
            stale_presented == 0
            and duplicate_playbacks == 0
            and required_manual_operations == 0
        ),
    )


class ArtifactEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    metric_results: dict[str, TargetEvaluation]


def evaluate_artifact(
    candidate: AggregateArtifact,
    baseline: AggregateArtifact,
) -> ArtifactEvaluation:
    if candidate.transport != "livekit" or baseline.transport != "websocket":
        raise ValueError("artifact comparison requires LiveKit and WebSocket")
    baseline_metrics = {metric.name: metric for metric in baseline.metrics}
    results: dict[str, TargetEvaluation] = {}
    for metric in candidate.metrics:
        if metric.name not in ABSOLUTE_LATENCY_LIMITS_MS or metric.p95 is None:
            continue
        websocket_metric = baseline_metrics.get(metric.name)
        websocket_p95 = (
            websocket_metric.p95 if websocket_metric is not None else None
        )
        results[metric.name] = evaluate_latency_target(
            metric_name=metric.name,
            p50_ms=metric.p50 if metric.p50 is not None else metric.p95,
            p95_ms=metric.p95,
            websocket_p95_ms=websocket_p95,
        )
    return ArtifactEvaluation(
        passed=bool(results) and all(result.passed for result in results.values()),
        metric_results=results,
    )

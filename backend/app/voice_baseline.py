from __future__ import annotations

import argparse
import json
import os
import platform
from pathlib import Path
from typing import Any, Sequence

from app.model_settings import resolve_model_settings
from app.stt.remote_whisper_client import WHISPER_COMPUTE_TYPE, WHISPER_DEVICE
from app.voice_metrics import (
    ClockMetadata,
    DiagnosticValue,
    HardwareMetadata,
    NetworkMetadata,
    ResourceMetadata,
    RunDiagnostics,
    TraceEvent,
    aggregate_events,
    create_run_metadata,
    validate_controlled_run,
)

FORBIDDEN_AGGREGATE_KEYS = {
    "event_id",
    "character_id",
    "session_id",
    "utterance_id",
    "response_id",
    "prompt",
    "transcript",
    "audio_body",
    "audio_payload",
}


def _required(
    value: dict[str, Any],
    key: str,
    expected: type | tuple[type, ...],
) -> Any:
    candidate = value.get(key)
    if not isinstance(candidate, expected):
        raise ValueError(f"baseline manifest {key} is invalid")
    return candidate


def _load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("baseline manifest must be an object")
    return value


def _load_trace(path: Path) -> list[TraceEvent]:
    events = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            events.append(TraceEvent.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"invalid trace event at line {line_number}") from exc
    return events


def _assert_anonymous(value: object) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            forbidden = FORBIDDEN_AGGREGATE_KEYS.intersection(current)
            if forbidden:
                raise ValueError(
                    f"aggregate contains forbidden keys: {sorted(forbidden)}"
                )
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def _write_human_report(artifact: dict[str, Any], path: Path) -> None:
    counts = artifact["run_counts"]
    lines = [
        "# WebSocket音声品質baseline v1",
        "",
        f"- warm-up: {counts['warmup']}",
        f"- 測定trial: {counts['measured']}",
        f"- 成功: {counts['success']}",
        f"- 失敗: {counts['failure']}",
        f"- 除外: {counts['excluded']}",
        "",
        "| 指標 | status | p50 | p95 | 成功 / trial |",
        "|---|---|---:|---:|---:|",
    ]
    for metric in artifact["metrics"]:
        p50 = "-" if metric["p50"] is None else f"{metric['p50']:.3f}"
        p95 = "-" if metric["p95"] is None else f"{metric['p95']:.3f}"
        lines.append(
            f"| {metric['name']} | {metric['status']} | {p50} | {p95} | "
            f"{metric['success_count']} / {metric['trial_count']} |"
        )
    lines.extend([
        "",
        "p50は診断値であり、latencyの比較・合否にはp95を使用する。",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def finalize_baseline(
    *,
    manifest_path: Path,
    trace_path: Path,
    output_path: Path,
    schema_path: Path,
) -> None:
    manifest = _load_manifest(manifest_path)
    trials = _required(manifest, "trials", list)
    fixture = _required(manifest, "fixture", dict)
    diagnostics_manifest = _required(manifest, "diagnostics", dict)
    warmups = [trial for trial in trials if trial.get("phase") == "warmup"]
    measured = [trial for trial in trials if trial.get("phase") == "measured"]
    if len(warmups) != 5 or len(measured) != 100:
        raise ValueError("baseline manifest requires five warmups and 100 trials")

    session_ids = [_required(trial, "sessionId", str) for trial in measured]
    utterance_ids = [_required(trial, "utteranceId", str) for trial in measured]
    response_ids = [_required(trial, "responseId", str) for trial in measured]
    conversation_ids = [_required(trial, "conversationId", str) for trial in measured]
    fixture_versions = [
        _required(trial, "fixture_version", str) for trial in trials
    ]
    fixture_version = _required(fixture, "fixture_version", str)
    if any(version != fixture_version for version in fixture_versions):
        raise ValueError("every trial fixture_version must match the fixture")
    audio_sha256 = _required(fixture, "audio_sha256", str)
    if len(audio_sha256) != 64 or any(character not in "0123456789abcdef" for character in audio_sha256):
        raise ValueError("baseline fixture audio_sha256 is invalid")
    if any(_required(trial, "audio_sha256", str) != audio_sha256 for trial in trials):
        raise ValueError("every trial audio_sha256 must match the fixture")
    if any(_required(trial, "transcript_matches", bool) is not True for trial in trials):
        raise ValueError("every controlled baseline transcript must match the fixture")
    sample_rate_hz = _required(fixture, "sample_rate_hz", int)
    speech_start_sample = _required(fixture, "speech_start_sample", int)
    speech_end_sample = _required(fixture, "speech_end_sample", int)
    if (
        sample_rate_hz <= 0
        or speech_start_sample < 0
        or speech_start_sample >= speech_end_sample
    ):
        raise ValueError("baseline fixture speech boundaries are invalid")
    initial_state_hashes = [
        _required(trial, "initial_state_hash", str) for trial in trials
    ]
    validate_controlled_run(
        warmup_runs=len(warmups),
        measured_runs=len(measured),
        session_ids=session_ids,
        conversation_ids=conversation_ids,
        fixture_versions=fixture_versions,
        initial_state_hashes=initial_state_hashes,
    )

    measured_keys = set(zip(session_ids, utterance_ids, response_ids, strict=True))
    events = [
        event
        for event in _load_trace(trace_path)
        if (event.session_id, event.utterance_id, event.response_id) in measured_keys
    ]
    character_by_trial = {
        (event.session_id, event.utterance_id, event.response_id): event.character_id
        for event in events
    }
    observed_keys = {
        (event.session_id, event.utterance_id, event.response_id) for event in events
    }
    if observed_keys != measured_keys:
        raise ValueError("controlled trace does not contain every measured trial")
    for index, trial in enumerate(measured):
        fixture_started_at = _required(trial, "fixtureStartedAt", (int, float))
        expected_speech_end = fixture_started_at + speech_end_sample * 1_000 / sample_rate_hz
        recorded_speech_end = _required(
            trial,
            "fixture_speech_end_client_ms",
            (int, float),
        )
        if recorded_speech_end != expected_speech_end:
            raise ValueError("trial fixture speech end does not match fixture metadata")
        for boundary, sample in (
            ("fixture_speech_start", speech_start_sample),
            ("fixture_speech_end", speech_end_sample),
        ):
            events.append(TraceEvent(
                schema_version="1.0",
                measurement_kind="controlled_baseline",
                event_id=f"{boundary}-{index}",
                character_id=character_by_trial[
                    (session_ids[index], utterance_ids[index], response_ids[index])
                ],
                session_id=session_ids[index],
                utterance_id=utterance_ids[index],
                response_id=response_ids[index],
                name=boundary,
                stage="fixture",
                outcome="success",
                timestamp=fixture_started_at + sample * 1_000 / sample_rate_hz,
                clock_domain="client_monotonic",
                unit="millisecond",
            ))

    unavailable = DiagnosticValue(
        status="not_applicable",
        reason="local_websocket_has_no_packet_loss_counter",
    )
    model_settings = resolve_model_settings(os.environ)
    metadata = create_run_metadata(
        measurement_kind="controlled_baseline",
        transport="websocket",
        run_id="websocket-baseline-v1",
        profile="integration-voice",
        fixture_version=fixture_version,
        warmup_runs=5,
        measured_runs=100,
        whisper_model=model_settings.whisper_model,
        whisper_device=WHISPER_DEVICE,
        whisper_compute_type=WHISPER_COMPUTE_TYPE,
    )
    diagnostics = RunDiagnostics(
        clocks=[
            ClockMetadata(domain="server_monotonic", method="perf_counter_ns", unit="nanosecond"),
            ClockMetadata(domain="client_monotonic", method="performance_now", unit="millisecond"),
        ],
        hardware=HardwareMetadata(description=platform.platform()),
        resources=ResourceMetadata(
            cpu_percent=DiagnosticValue(
                status="measured",
                value=_required(diagnostics_manifest, "cpu_percent", (int, float)),
            ),
            memory_bytes=DiagnosticValue(
                status="measured",
                value=_required(diagnostics_manifest, "maximum_resident_set_bytes", int),
            ),
        ),
        network=NetworkMetadata(
            sent_bytes=DiagnosticValue(
                status="measured",
                value=sum(_required(trial, "sentBytes", int) for trial in measured),
            ),
            received_bytes=DiagnosticValue(
                status="measured",
                value=sum(_required(trial, "receivedBytes", int) for trial in measured),
            ),
            packet_loss_basis_points=unavailable,
            condition="Ubuntu-dev localhost WebSocket",
        ),
    )
    artifact = aggregate_events(events, metadata=metadata, diagnostics=diagnostics)
    if artifact.run_counts.measured != 100:
        raise ValueError("aggregate must contain exactly 100 measured trials")
    serialized = artifact.model_dump(mode="json")
    _assert_anonymous(serialized)
    from jsonschema import Draft202012Validator

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(serialized)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(serialized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_human_report(serialized, output_path.with_suffix(".md"))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--manifest", type=Path, required=True)
    finalize.add_argument("--trace", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.add_argument("--schema", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "finalize":
        finalize_baseline(
            manifest_path=args.manifest,
            trace_path=args.trace,
            output_path=args.output,
            schema_path=args.schema,
        )


if __name__ == "__main__":
    main()

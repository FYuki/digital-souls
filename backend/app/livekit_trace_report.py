from __future__ import annotations

import argparse
import json
import os
import platform
from pathlib import Path
from typing import Sequence

from jsonschema import Draft202012Validator

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
)

_FORBIDDEN_KEYS = {
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


def _load_traces(paths: Sequence[Path]) -> list[TraceEvent]:
    events: list[TraceEvent] = []
    for path in paths:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            try:
                events.append(TraceEvent.model_validate_json(line))
            except ValueError as error:
                raise ValueError(
                    f"invalid trace event: {path}:{line_number}"
                ) from error
    if not events:
        raise ValueError("LiveKit report requires at least one trace event")
    if any(event.measurement_kind != "dogfood" for event in events):
        raise ValueError("LiveKit dogfood report accepts only dogfood traces")
    return events


def _assert_anonymous(value: object) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            forbidden = _FORBIDDEN_KEYS.intersection(current)
            if forbidden:
                raise ValueError(
                    f"aggregate contains forbidden keys: {sorted(forbidden)}"
                )
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def _write_report(value: dict[str, object], output_path: Path) -> None:
    counts = value["run_counts"]
    metrics = value["metrics"]
    assert isinstance(counts, dict)
    assert isinstance(metrics, list)
    lines = [
        "# LiveKit dogfood音声品質レポート",
        "",
        f"- 測定trial: {counts['measured']}",
        f"- 成功: {counts['success']}",
        f"- 失敗: {counts['failure']}",
        f"- 除外: {counts['excluded']}",
        "",
        "| 指標 | status | p50 | p95 | 成功 / trial |",
        "|---|---|---:|---:|---:|",
    ]
    for candidate in metrics:
        assert isinstance(candidate, dict)
        p50 = candidate["p50"]
        p95 = candidate["p95"]
        p50_text = "-" if p50 is None else f"{float(p50):.3f}"
        p95_text = "-" if p95 is None else f"{float(p95):.3f}"
        lines.append(
            f"| {candidate['name']} | {candidate['status']} | {p50_text} | "
            f"{p95_text} | {candidate['success_count']} / {candidate['trial_count']} |"
        )
    output_path.with_suffix(".md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def finalize_livekit_dogfood_report(
    *,
    trace_paths: Sequence[Path],
    output_path: Path,
    schema_path: Path,
    run_id: str,
) -> None:
    events = _load_traces(trace_paths)
    trial_count = len({
        (event.session_id, event.utterance_id, event.response_id)
        for event in events
    })
    model_settings = resolve_model_settings(os.environ)
    metadata = create_run_metadata(
        measurement_kind="dogfood",
        transport="livekit",
        run_id=run_id,
        profile="dogfood",
        fixture_version="manual-dogfood-v1",
        warmup_runs=0,
        measured_runs=trial_count,
        whisper_model=model_settings.whisper_model,
        whisper_device=WHISPER_DEVICE,
        whisper_compute_type=WHISPER_COMPUTE_TYPE,
    )
    unavailable = DiagnosticValue(
        status="missing",
        reason="dogfood_resource_sample_not_recorded",
    )
    diagnostics = RunDiagnostics(
        clocks=[
            ClockMetadata(
                domain="server_monotonic",
                method="perf_counter_ns",
                unit="nanosecond",
            ),
            ClockMetadata(
                domain="client_monotonic",
                method="performance_now",
                unit="millisecond",
            ),
        ],
        hardware=HardwareMetadata(description=platform.platform()),
        resources=ResourceMetadata(
            cpu_percent=unavailable,
            memory_bytes=unavailable,
        ),
        network=NetworkMetadata(
            sent_bytes=unavailable,
            received_bytes=unavailable,
            packet_loss_basis_points=unavailable,
            condition="Ubuntu-dogfood LiveKit",
        ),
    )
    artifact = aggregate_events(events, metadata=metadata, diagnostics=diagnostics)
    serialized = artifact.model_dump(mode="json")
    _assert_anonymous(serialized)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(serialized)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(serialized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_report(serialized, output_path)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    finalize_livekit_dogfood_report(
        trace_paths=args.trace,
        output_path=args.output,
        schema_path=args.schema,
        run_id=args.run_id,
    )


if __name__ == "__main__":
    main()

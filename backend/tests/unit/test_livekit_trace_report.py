from __future__ import annotations

import json
from pathlib import Path

from app.livekit_trace_report import finalize_livekit_dogfood_report
from app.voice_metrics import TraceEvent


def test_livekit_dogfood_report_aggregates_anonymous_trace(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    events = [
        TraceEvent(
            schema_version="1.0",
            measurement_kind="dogfood",
            event_id=f"event-{index}",
            session_id="session-1",
            utterance_id="utterance-1",
            response_id="response-1",
            name=name,
            stage=stage,
            outcome="success",
            timestamp=timestamp,
            clock_domain=clock_domain,
            unit=unit,
        )
        for index, (name, stage, timestamp, clock_domain, unit) in enumerate(
            (
                ("speech_stopped", "vad", 1_000, "client_monotonic", "millisecond"),
                (
                    "utterance_finalized",
                    "stt",
                    1_500_000_000,
                    "server_monotonic",
                    "nanosecond",
                ),
                ("first_playback", "playback", 1_750, "client_monotonic", "millisecond"),
            )
        )
    ]
    trace_path.write_text(
        "".join(event.model_dump_json() + "\n" for event in events),
        encoding="utf-8",
    )
    output_path = tmp_path / "livekit-dogfood.json"
    schema_path = (
        Path(__file__).resolve().parents[3]
        / "docs/schemas/voice-quality-artifact-v1.schema.json"
    )

    finalize_livekit_dogfood_report(
        trace_paths=[trace_path],
        output_path=output_path,
        schema_path=schema_path,
        run_id="dogfood-2026-09-01",
    )

    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    serialized = json.dumps(artifact)
    assert artifact["measurement_kind"] == "dogfood"
    assert artifact["transport"] == "livekit"
    assert artifact["run_counts"]["measured"] == 1
    assert next(
        metric for metric in artifact["metrics"] if metric["name"] == "ttfa"
    )["p50"] == 750.0
    assert all(
        forbidden not in serialized
        for forbidden in ("event-1", "session-1", "utterance-1", "response-1")
    )
    assert output_path.with_suffix(".md").exists()

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict
import json
from pathlib import Path
from threading import Event

import httpx
import pytest

from app.async_worker import run_sync
from app.conversation_core import ConversationCoreSession, TextDelta
from app.inference.adapters.ollama import OllamaAdapter
from app.inference.contracts import InferenceMessage, TextGenerationRequest
from app.inference.diagnostics import (
    InferenceDiagnostics, collect_diagnostics, diagnostic,
    estimate_diagnostics, ollama_diagnostics,
)
from app.livekit_trace_report import finalize_livekit_dogfood_report
from app.livekit_transport.measurement import LiveKitMeasurementSession
from tests.conversation_core_test_support import (
    RecordingDelivery, RecordingPersistence, RecordingStt, RecordingTts,
    response_id_factory,
)


def test_provider_durations_preserve_missing_values_and_ignore_payload() -> None:
    with collect_diagnostics() as collector:
        with estimate_diagnostics():
            ollama_diagnostics({
                "total_duration": 5_000_000, "load_duration": 0,
                "prompt_eval_count": 12, "eval_duration": -1,
                "prompt_eval_duration": True, "eval_count": "private",
                "message": {"content": "private"},
            })
        ollama_diagnostics({"total_duration": 9_000_000, "eval_count": 3})
        events = {event.name: event.value for event in collector.finish()}
    assert events == {
        "ollama_estimate_total_ms": 5,
        "ollama_estimate_load_ms": 0,
        "ollama_estimate_input_tokens": 12,
        "ollama_generation_total_ms": 9,
        "ollama_generation_output_tokens": 3,
    }
    assert "private" not in json.dumps(events)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "private", None])
def test_numeric_diagnostics_reject_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        InferenceDiagnostics().record("token_estimate_total_ms", value)  # type: ignore[arg-type]


def test_points_keep_first_timestamp_and_numeric_requests_accumulate() -> None:
    with collect_diagnostics() as collector:
        diagnostic("llm_first_token")
        first = collector._events["llm_first_token"]
        diagnostic("llm_first_token")
        diagnostic("token_estimate_requests", 1)
        diagnostic("token_estimate_requests", 1)
        diagnostic("token_estimate_total_ms", 1.5)
        diagnostic("token_estimate_total_ms", 2.5)
        events = {event.name: event for event in collector.finish()}
    assert events["llm_first_token"] == first
    assert events["token_estimate_requests"].value == 2
    assert events["token_estimate_total_ms"].value == 4
    with pytest.raises(ValueError):
        collector.record("private")


def test_concurrent_scopes_and_anyio_workers_do_not_mix() -> None:
    async def exercise() -> None:
        async def response(count: int) -> tuple[object, ...]:
            with collect_diagnostics() as collector:
                await run_sync(diagnostic, "prompt_input_tokens", count)
                await asyncio.sleep(0)
                return collector.finish()
        left, right = await asyncio.gather(response(10), response(20))
        assert [event.value for event in left] == [10]
        assert [event.value for event in right] == [20]
        with collect_diagnostics() as unrelated:
            assert unrelated.finish() == ()
    asyncio.run(exercise())


def test_cancelled_scope_ignores_abandoned_worker_result() -> None:
    started, release, finished = Event(), Event(), Event()
    collector = None

    def late_worker() -> None:
        started.set()
        release.wait(timeout=2)
        diagnostic("token_estimate_requests", 1)
        finished.set()

    async def response() -> None:
        nonlocal collector
        with collect_diagnostics() as collector:
            diagnostic("prompt_preparation_started")
            await run_sync(late_worker)

    async def exercise() -> None:
        task = asyncio.create_task(response())
        try:
            while not started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            while not finished.is_set():
                await asyncio.sleep(0)
            assert collector is not None
            assert [event.name for event in collector.finish()] == ["prompt_preparation_started"]
        finally:
            release.set()
    asyncio.run(exercise())


def test_real_adapter_stream_reports_headers_and_terminal_provider_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.AsyncClient
    chunks = [
        {"message": {"content": "", "thinking": "private reasoning"}, "done": False},
        {"message": {"content": "hello"}, "done": False},
        {"message": {"content": ""}, "done": True,
         "total_duration": 12_000_000, "load_duration": 2_000_000,
         "prompt_eval_duration": 4_000_000, "eval_duration": 6_000_000,
         "prompt_eval_count": 12, "eval_count": 3},
    ]
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content="\n".join(json.dumps(chunk) for chunk in chunks),
    ))
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: original(transport=transport, **kwargs),
    )
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434")
    request = TextGenerationRequest(
        messages=(InferenceMessage("user", "private input"),),
        model_id="gemma4:e4b", options={}, max_input_tokens=100,
        max_output_tokens=10, timeout_seconds=2,
    )

    async def exercise() -> None:
        with collect_diagnostics() as collector:
            assert [part async for part in adapter.stream_text(request)] == ["hello"]
            events = {event.name: event for event in collector.finish()}
        assert events["llm_http_headers_received"].timestamp_ns >= events["llm_http_started"].timestamp_ns
        assert events["ollama_internal_timing_unavailable"].value is None
        assert events["ollama_generation_total_ms"].value == 12
        assert events["llm_thinking_chunks"].value == 1
        assert events["llm_thinking_characters"].value == len("private reasoning")
        assert events["llm_first_provider_chunk"].timestamp_ns <= events["llm_first_thinking_chunk"].timestamp_ns
        assert events["ollama_generation_load_ms"].value == 2
        assert "private" not in json.dumps([asdict(event) for event in events.values()])
    try:
        asyncio.run(exercise())
    finally:
        adapter.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_core_diagnostics_reach_correlated_trace_and_anonymous_report(
    tmp_path: Path, cancel: bool,
) -> None:
    events = []
    measurement = LiveKitMeasurementSession(
        session_id="session-test", character_id="character-test",
        measurement_kind="dogfood", record=events.append, clock_ns=lambda: 10,
    )
    measurement.bind_response(
        response_id="response-test", source_utterance_ids=("utterance-test",),
    )
    generated = asyncio.Event()
    release = asyncio.Event()

    class Llm:
        async def generate(self, _transcript: str) -> AsyncIterator[TextDelta]:
            diagnostic("ollama_internal_timing_unavailable")
            diagnostic("llm_http_started")
            diagnostic("llm_first_token")
            diagnostic("token_estimate_requests", 3)
            diagnostic("token_estimate_total_ms", 450)
            yield TextDelta(1, "合成の返答。", (0, 6))
            generated.set()
            if cancel:
                await release.wait()
            diagnostic("llm_stream_completed")

    async def exercise() -> None:
        session = ConversationCoreSession(
            session_id="session-test",
            response_id_factory=response_id_factory("response-test"),
            delivery=RecordingDelivery(), persistence=RecordingPersistence(),
            observation=measurement, stt=RecordingStt(), llm=Llm(), tts=RecordingTts(),
        )
        await session.finalize_utterance(
            utterance_id="utterance-test", transcript="合成の質問",
            should_response=True,
        )
        await asyncio.wait_for(generated.wait(), timeout=1)
        if cancel:
            await session.end()
        while session.running_stage_count:
            await asyncio.sleep(0)
    asyncio.run(exercise())
    diagnostic_events = [event for event in events if event.stage == "inference_diagnostic"]
    assert diagnostic_events
    assert all(
        (event.session_id, event.utterance_id, event.response_id)
        == ("session-test", "utterance-test", "response-test")
        for event in diagnostic_events
    )
    assert next(e for e in diagnostic_events if e.name == "llm_first_token").timestamp > 10
    assert any(e.name == "llm_stream_completed" for e in diagnostic_events) is not cancel
    trace = tmp_path / "trace.jsonl"
    trace.write_text("".join(event.model_dump_json() + "\n" for event in events))
    output = tmp_path / "aggregate.json"
    finalize_livekit_dogfood_report(
        trace_paths=[trace], output_path=output,
        schema_path=Path(__file__).resolve().parents[3] / "docs/schemas/voice-quality-artifact-v1.schema.json",
        run_id="diagnostic-test",
    )
    report = json.loads(output.read_text())
    metrics = {metric["name"]: metric for metric in report["metrics"]}
    assert metrics["token_estimate_total_ms"]["p95"] == 450
    assert metrics["token_estimate_requests"]["p95"] == 3
    assert metrics["ollama_generation_load_ms"]["status"] == "missing"
    for name in ("llm_provider_acceptance_latency", "llm_provider_generation_start_latency",
                 "llm_generation_start_to_first_token_received", "ollama_provider_queue_wait"):
        assert metrics[name]["missing_outcomes"] == {"ollama_api_internal_timing_not_exposed": 1}
        assert metrics[name]["missing_count"] == metrics[name]["rate_denominator"] == 1

    assert all(secret not in output.read_text() for secret in (
        "character-test", "session-test", "utterance-test", "response-test", "合成の返答",
    ))

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
from threading import Lock
from time import perf_counter_ns


# 名前を固定し、本文・providerの任意キーがtraceへ入る経路を作らない。
POINT_NAMES = frozenset({
    "prompt_preparation_started", "prompt_preparation_completed",
    "llm_request_started", "llm_capacity_acquired", "llm_http_started",
    "llm_http_headers_received", "llm_first_token", "llm_stream_completed",
    "llm_first_provider_chunk", "llm_first_thinking_chunk",
})
_PROVIDER_FIELDS = {
    "total_duration": "total_ms",
    "load_duration": "load_ms",
    "prompt_eval_duration": "prompt_eval_ms",
    "eval_duration": "generation_ms",
    "prompt_eval_count": "input_tokens",
    "prompt_eval_cached_count": "cached_input_tokens",
    "eval_count": "output_tokens",
}
VALUE_NAMES = frozenset({
    "token_estimate_requests", "token_estimate_total_ms", "token_estimate_queue_ms",
    "prompt_message_count", "prompt_input_tokens",
    "llm_thinking_chunks", "llm_thinking_characters",
    "token_estimate_cache_hits", "token_estimate_metadata_requests",
    *(f"prompt_{part}_tokens" for part in ("character", "character_lore", "history", "rag", "current_user", "post_history")),
    *(
        f"ollama_{operation}_{suffix}"
        for operation in ("estimate", "generation")
        for suffix in _PROVIDER_FIELDS.values()
    ),
})
DIAGNOSTIC_NAMES = POINT_NAMES | VALUE_NAMES


@dataclass(frozen=True)
class DiagnosticEvent:
    name: str
    timestamp_ns: int
    value: float | None = None


class InferenceDiagnostics:
    """1応答の数値だけを固定サイズで保持し、cancel後のworker結果を捨てる。"""

    def __init__(self) -> None:
        self._events: dict[str, DiagnosticEvent] = {}
        self._lock = Lock()
        self._closed = False

    def record(self, name: str, value: float | None = None) -> None:
        if name not in DIAGNOSTIC_NAMES:
            raise ValueError("unknown inference diagnostic")
        if name in POINT_NAMES:
            if value is not None:
                raise ValueError("point diagnostic does not accept a value")
        elif (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0
        ):
            raise ValueError("numeric diagnostic requires a finite non-negative value")
        with self._lock:
            if self._closed:
                return
            existing = self._events.get(name)
            if existing is not None:
                if value is None:
                    return
                value += existing.value or 0
                if not math.isfinite(value):
                    return
            self._events[name] = DiagnosticEvent(name, perf_counter_ns(), value)

    def finish(self) -> tuple[DiagnosticEvent, ...]:
        with self._lock:
            self._closed = True
            return tuple(self._events.values())


_CURRENT: ContextVar[InferenceDiagnostics | None] = ContextVar(
    "inference_diagnostics", default=None,
)
_OPERATION: ContextVar[str] = ContextVar("inference_operation", default="generation")


@contextmanager
def collect_diagnostics() -> Iterator[InferenceDiagnostics]:
    collector = InferenceDiagnostics()
    token = _CURRENT.set(collector)
    try:
        yield collector
    finally:
        collector.finish()
        _CURRENT.reset(token)


@contextmanager
def estimate_diagnostics() -> Iterator[None]:
    token = _OPERATION.set("estimate")
    try:
        yield
    finally:
        _OPERATION.reset(token)


def diagnostic(name: str, value: float | None = None) -> None:
    collector = _CURRENT.get()
    if collector is not None:
        collector.record(name, value)


def ollama_diagnostics(body: Mapping[str, object]) -> None:
    if _CURRENT.get() is None:
        return
    for field, suffix in _PROVIDER_FIELDS.items():
        value = body.get(field)
        # 欠落値、不正値を0と見せず、provider任意文字列も取り込まない。
        if type(value) is not int or value < 0:
            continue
        try:
            numeric = float(value) / (1_000_000 if field.endswith("duration") else 1)
        except OverflowError:
            continue
        if math.isfinite(numeric):
            diagnostic(f"ollama_{_OPERATION.get()}_{suffix}", numeric)

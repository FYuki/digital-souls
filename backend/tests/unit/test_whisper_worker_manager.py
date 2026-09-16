from __future__ import annotations

import threading

import pytest

from whisper_service.config import WhisperServiceConfig, load_config
from whisper_service.worker import (
    WhisperCapacityError,
    WhisperInferenceTimeoutError,
    WhisperWorkerManager,
)


def _config(*, timeout: float = 45.0) -> WhisperServiceConfig:
    return WhisperServiceConfig(
        model="medium",
        model_revision="a" * 40,
        model_path="/opt/models/whisper-medium",
        model_cache="/models/whisper",
        inference_timeout_seconds=timeout,
    )


class _AliveWorker:
    def __init__(self) -> None:
        self.terminated = False

    def is_alive(self) -> bool:
        return not self.terminated

    def join(self, timeout: float) -> None:
        del timeout

    def terminate(self) -> None:
        self.terminated = True


class _BlockingConnection:
    def __init__(self) -> None:
        self.sent = threading.Event()
        self.release = threading.Event()
        self.request_id = 0

    def send(self, payload: object) -> None:
        if payload is None:
            return
        self.request_id = payload[0]  # type: ignore[index]
        self.sent.set()

    def poll(self, timeout: float) -> bool:
        return self.release.wait(timeout)

    def recv(self) -> tuple[int, str, str]:
        return (self.request_id, "ok", "文字起こし")

    def close(self) -> None:
        return None


def _ready_manager(connection: object, *, timeout: float = 45.0) -> WhisperWorkerManager:
    manager = WhisperWorkerManager(_config(timeout=timeout))
    manager._worker = _AliveWorker()  # type: ignore[assignment]
    manager._connection = connection  # type: ignore[assignment]
    manager._ready = True
    return manager


def test_should_fail_fast_when_the_single_gpu_slot_is_busy() -> None:
    connection = _BlockingConnection()
    manager = _ready_manager(connection)
    result: list[str] = []
    first = threading.Thread(target=lambda: result.append(manager.transcribe(b"\0\0")))
    first.start()
    assert connection.sent.wait(timeout=1)

    with pytest.raises(WhisperCapacityError):
        manager.transcribe(b"\0\0")

    connection.release.set()
    first.join(timeout=1)
    assert result == ["文字起こし"]
    manager.close()


def test_should_discard_and_schedule_recovery_after_timeout() -> None:
    connection = _BlockingConnection()
    manager = _ready_manager(connection, timeout=0.001)
    recovered: list[bool] = []
    manager._recover_async = lambda: recovered.append(True)  # type: ignore[method-assign]

    with pytest.raises(WhisperInferenceTimeoutError):
        manager.transcribe(b"\0\0")

    assert manager.ready is False
    assert recovered == [True]


def test_should_reject_runtime_revision_different_from_baked_model() -> None:
    with pytest.raises(ValueError, match="must match"):
        load_config(
            {
                "WHISPER_MODEL_REVISION": "a" * 40,
                "WHISPER_BAKED_MODEL_REVISION": "b" * 40,
            }
        )


@pytest.mark.parametrize("recognized", ["", "うん", "止めて", "ご視聴ありがとうございました"])
def test_worker_filters_non_speech_but_preserves_recognized_words(monkeypatch, recognized: str) -> None:
    """warmupの幻覚を公開せず、文字列が同じでも音声の証拠で区別する。"""
    import sys
    import types

    from whisper_service.worker import _worker_main

    consumed: list[bool] = []

    class Model:
        def __init__(self, *args, **kwargs):
            self.calls = 0

        def transcribe(self, source, *, language: str):
            assert language == "ja"
            self.calls += 1
            warmup = self.calls == 1

            def segments():
                consumed.append(warmup)
                yield types.SimpleNamespace(text="ご視聴ありがとうございました", no_speech_prob=0.9)
                if recognized and not warmup:
                    yield types.SimpleNamespace(text=recognized, no_speech_prob=0.1)

            return segments(), object()

    class Connection:
        def __init__(self):
            self.requests = iter([(7, b"\0\0" * 1600), None])
            self.sent = []
            self.closed = False

        def recv(self):
            return next(self.requests)

        def send(self, value):
            self.sent.append(value)

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    connection = Connection()
    _worker_main(connection, _config())
    assert consumed == [True, False]
    assert connection.sent == [("ready",), (7, "ok", recognized)]
    assert connection.closed


@pytest.mark.parametrize("probability", [0.600001, 1.0, float("nan"), float("inf"), -0.1, 1.1])
def test_rejects_non_speech_and_invalid_evidence(probability: float) -> None:
    from types import SimpleNamespace
    from whisper_service.worker import _speech_text

    assert _speech_text([SimpleNamespace(text="定型句", no_speech_prob=probability)]) == ""


def test_keeps_speech_at_threshold_without_trimming_text() -> None:
    from types import SimpleNamespace
    from whisper_service.worker import _speech_text

    assert _speech_text([SimpleNamespace(text="はい、でも質問があります。", no_speech_prob=0.6)]) == "はい、でも質問があります。"

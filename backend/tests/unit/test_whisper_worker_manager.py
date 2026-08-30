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

from __future__ import annotations

import asyncio
import json

import httpx

from whisper_service.app import _transcribe_response, create_app
from whisper_service.config import WhisperServiceConfig
from whisper_service.worker import (
    WhisperCapacityError,
    WhisperInferenceTimeoutError,
    WhisperNotReadyError,
)


class FakeManager:
    def __init__(self) -> None:
        self.config = WhisperServiceConfig(
            model="medium",
            model_revision="revision-sha256",
            model_path="/opt/models/whisper-medium",
            model_cache="/models/whisper",
            inference_timeout_seconds=45,
        )
        self.ready = False
        self.error: Exception | None = None
        self.calls: list[bytes] = []

    def start(self) -> None:
        self.ready = True

    def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        if self.error is not None:
            raise self.error
        return "音声入力"

    def close(self) -> None:
        self.ready = False


def _request(app, method: str, path: str, **kwargs) -> httpx.Response:
    async def send() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://whisper.test",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def test_should_publish_metadata_only_health_and_version() -> None:
    manager = FakeManager()
    manager.start()
    app = create_app(manager)
    assert _request(app, "GET", "/health/live").json() == {"status": "live"}
    assert _request(app, "GET", "/health/ready").json() == {"status": "ready"}
    version = _request(app, "GET", "/version").json()
    assert version == {
        "serviceVersion": "1.0",
        "model": "medium",
        "modelRevision": "revision-sha256",
        "device": "cuda",
        "deviceIndex": 0,
        "computeType": "int8_float16",
        "modelInstances": 1,
        "globalInflight": 1,
    }


def test_should_accept_only_pcm16_octet_stream_without_echoing_body() -> None:
    manager = FakeManager()
    manager.start()
    response = _transcribe_response(manager, b"\x00\x00")
    app = create_app(manager)
    invalid = _request(
        app,
        "POST",
        "/v1/transcriptions",
        content=b"secret transcript",
        headers={"Content-Type": "text/plain"},
    )
    assert json.loads(response.body) == {"text": "音声入力"}
    assert manager.calls == [b"\x00\x00"]
    assert invalid.status_code == 415
    assert "secret transcript" not in invalid.text


def test_should_fail_readiness_without_a_ready_gpu_worker() -> None:
    manager = FakeManager()
    app = create_app(manager)
    manager.ready = False
    response = _request(app, "GET", "/health/ready")
    assert response.status_code == 503


def test_should_map_capacity_timeout_and_not_ready_without_payload() -> None:
    manager = FakeManager()
    cases = (
        (WhisperCapacityError("busy"), 429, "stt_capacity_exceeded"),
        (WhisperInferenceTimeoutError("timeout"), 504, "stt_inference_timeout"),
        (WhisperNotReadyError("not ready"), 503, "stt_not_ready"),
    )
    for error, status, code in cases:
        manager.error = error
        response = _transcribe_response(manager, b"\x00\x00")
        assert response.status_code == status
        assert json.loads(response.body) == {"error": {"code": code}}


def test_config_has_no_cpu_fallback_setting() -> None:
    config = WhisperServiceConfig(
        model="medium",
        model_revision="revision",
        model_path="/opt/models/whisper-medium",
        model_cache="/models/whisper",
        inference_timeout_seconds=45,
    )
    assert config.device == "cuda"
    assert config.compute_type == "int8_float16"
    assert "fallback" not in config.__dataclass_fields__

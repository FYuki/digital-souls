from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from whisper_service.config import WhisperServiceConfig, load_config
from whisper_service.worker import (
    WhisperCapacityError,
    WhisperInferenceTimeoutError,
    WhisperNotReadyError,
    WhisperServiceError,
    WhisperWorkerManager,
)


SERVICE_VERSION = "1.0"
MAX_AUDIO_BYTES = 16_000 * 2 * 60


class TranscriptionManager(Protocol):
    config: WhisperServiceConfig

    @property
    def ready(self) -> bool: ...

    def start(self) -> None: ...

    def transcribe(self, audio: bytes) -> str: ...

    def close(self) -> None: ...


def create_app(manager: TranscriptionManager | None = None) -> FastAPI:
    resolved_manager = manager or WhisperWorkerManager(load_config())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        resolved_manager.start()
        try:
            yield
        finally:
            resolved_manager.close()

    app = FastAPI(title="Digital Souls Whisper Service", lifespan=lifespan)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        status = "ready" if resolved_manager.ready else "not_ready"
        return JSONResponse({"status": status}, status_code=200 if resolved_manager.ready else 503)

    @app.get("/version")
    async def version() -> dict[str, object]:
        config = resolved_manager.config
        return {
            "serviceVersion": SERVICE_VERSION,
            "model": config.model,
            "modelRevision": config.model_revision,
            "device": config.device,
            "deviceIndex": config.device_index,
            "computeType": config.compute_type,
            "modelInstances": 1,
            "globalInflight": 1,
        }

    @app.post("/v1/transcriptions")
    async def transcriptions(request: Request) -> JSONResponse:
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
            return _error(415, "invalid_audio_content_type")
        audio = await request.body()
        if not audio or len(audio) > MAX_AUDIO_BYTES or len(audio) % 2 != 0:
            return _error(422, "invalid_pcm16_audio")
        return await run_in_threadpool(_transcribe_response, resolved_manager, audio)

    return app


def _transcribe_response(
    manager: TranscriptionManager, audio: bytes
) -> JSONResponse:
    try:
        transcript = manager.transcribe(audio)
    except WhisperCapacityError as error:
        return _error(429, error.error_code)
    except WhisperInferenceTimeoutError as error:
        return _error(504, error.error_code)
    except WhisperNotReadyError as error:
        return _error(503, error.error_code)
    except WhisperServiceError as error:
        return _error(502, error.error_code)
    return JSONResponse({"text": transcript})


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code}}, status_code=status_code)


app = create_app()

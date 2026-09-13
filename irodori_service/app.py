from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from irodori_service.config import (
    CODEC_REVISION,
    IRODORI_REVISION,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    SERVER_REVISION,
    ServiceConfig,
    load_config,
)
from irodori_service.contracts import ServiceError, SpeechRequest
from irodori_service.scheduler import Environment, SynthesisScheduler, SynthesisWorker
from irodori_service.voices import RegisteredVoices
from irodori_service.worker import ProcessWorker

MAX_REQUEST_BYTES = 64 * 1024


def create_app(
    config: ServiceConfig | None = None, worker: SynthesisWorker | None = None,
) -> FastAPI:
    config = config or load_config()
    worker = worker or ProcessWorker(config)
    voices = RegisteredVoices(config.voices_dir)
    scheduler = SynthesisScheduler(worker, config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            await scheduler.start()
            yield
        finally:
            await scheduler.close()

    app = FastAPI(title="Digital Souls Irodori Service", lifespan=lifespan)
    app.state.scheduler = scheduler

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, error: ServiceError) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code}}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": {"code": "tts_invalid_request"}}, status_code=422)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        available = scheduler.ready
        return JSONResponse(
            {"status": "ready" if available else "not_ready"},
            status_code=200 if available else 503,
        )

    @app.get("/version")
    async def version() -> dict[str, object]:
        return {
            "serviceVersion": "1.0", "model": MODEL_REPOSITORY,
            "modelRevision": MODEL_REVISION, "codecRevision": CODEC_REVISION,
            "serverRevision": SERVER_REVISION, "irodoriRevision": IRODORI_REVISION,
            "device": "cuda", "precision": "bf16", "quantized": False,
            "globalInflight": 1, "maxPending": config.max_pending,
            "queueTimeoutSeconds": config.queue_timeout,
            "inferenceTimeoutSeconds": config.inference_timeout,
            "preparationSeconds": getattr(worker, "preparation_seconds", None),
            "active": scheduler.active_count, "pending": scheduler.pending_count,
        }

    @app.get("/v1/audio/voices")
    async def list_voices() -> dict[str, object]:
        identifiers = await asyncio.to_thread(voices.list_ids)
        return {"object": "list", "data": [
            {"id": identifier, "object": "voice", "no_ref": False}
            for identifier in identifiers
        ]}

    @app.post("/v1/audio/speech")
    async def speech(request: Request) -> Response:
        environment = request.headers.get("X-DS-Environment")
        if environment not in {"dev", "test", "dogfood"}:
            raise ServiceError("tts_environment_invalid", 422)
        # 大きな本文を無制限にメモリへ読み込まず、validation例外へ本文も載せない。
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                raise ServiceError("tts_request_too_large", 413)
        try:
            payload = SpeechRequest.model_validate_json(body)
        except ValueError as error:
            raise ServiceError("tts_invalid_request", 422) from error
        await asyncio.to_thread(voices.resolve, payload.voice)
        result = asyncio.create_task(scheduler.submit(payload, cast(Environment, environment)))

        async def watch_disconnect() -> None:
            while not await request.is_disconnected():
                await asyncio.sleep(0.025)

        disconnected = asyncio.create_task(watch_disconnect())
        try:
            done, _ = await asyncio.wait(
                {result, disconnected}, return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnected in done:
                result.cancel()
                raise ServiceError("tts_request_cancelled", 499)
            return Response(await result, media_type="audio/wav")
        finally:
            disconnected.cancel()
            if not result.done():
                result.cancel()
            await asyncio.gather(result, disconnected, return_exceptions=True)

    return app


app = create_app()

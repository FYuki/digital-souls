"""切断probeが取消を吸収してもHTTP応答を終了する回帰試験。"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from starlette.requests import Request

from irodori_service.contracts import ServiceError


@pytest.mark.parametrize("fails", [False, True])
def test_finished_synthesis_does_not_wait_for_client_disconnect(monkeypatch, fails):
    service = importlib.import_module("irodori_service.app")

    class VoiceChecks:
        def __init__(self, limit):
            pass

        async def run(self, operation):
            return operation()

    monkeypatch.setattr(service, "VoiceValidationPool", VoiceChecks)
    monkeypatch.setattr(service.RegisteredVoices, "resolve", lambda *_: None)

    async def run():
        entered = asyncio.Event()
        disconnected = asyncio.Event()
        swallowed = asyncio.Event()
        app = service.create_app()

        class ProbeRequest(Request):
            async def stream(self):
                yield b'{"input":"fixture","voice":"fixture","irodori":{"caption":"fixture","seed":42}}'

            async def is_disconnected(self):
                entered.set()
                try:
                    await disconnected.wait()
                    return True
                except asyncio.CancelledError:
                    # 実Starlette/AnyIOで再現した、probe内の外部取消吸収を模擬する。
                    swallowed.set()
                    return False

        async def synthesize(*args):
            await entered.wait()
            if fails:
                raise ServiceError("tts_inference_timeout", 504)
            return b"synthetic-audio"

        app.state.scheduler.submit = synthesize
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/v1/audio/speech")
        request = ProbeRequest({"type": "http", "headers": [(b"x-ds-environment", b"test")]})
        task = asyncio.create_task(endpoint(request))
        try:
            done, _ = await asyncio.wait({task}, timeout=1)
            assert swallowed.is_set()
            assert task in done, "合成済み応答がクライアント切断待ちになった"
            if fails:
                with pytest.raises(ServiceError, match="tts_inference_timeout"):
                    await task
            else:
                response = await task
                assert response.status_code == 200
                assert response.body == b"synthetic-audio"
        finally:
            disconnected.set()
            await asyncio.wait({task}, timeout=1)
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())

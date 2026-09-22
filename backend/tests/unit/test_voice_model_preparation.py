from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from threading import BoundedSemaphore
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.inference.adapters.ollama import OllamaAdapter
from app.inference.authorization import InferenceCaller
from app.inference.contracts import InferenceCapability, InferenceTarget, ModelPreparationRequest
from app.inference.errors import InferenceError, InferenceErrorCategory
from app.livekit_transport.preparation import VoiceModelPreparationError
from app.stt.remote_whisper_client import RemoteWhisperTimeoutError
from tests.unit.test_inference_core import _FakeAdapter, _router

CHAT = {"caller": InferenceCaller.CHAT, "target": InferenceTarget.CHAT}


def request():
    return ModelPreparationRequest("gemma4:e4b", {"temperature": 0.2}, 7168, 1024, 2, True)


@pytest.mark.parametrize("response_body", [
    {"done": True, "done_reason": "load"}, {"done": False},
    {"done": True, "done_reason": "stop"}, {"done": "true", "done_reason": "load"},
])
def test_ollama_prepares_without_messages_using_the_real_chat_context(response_body):
    async def scenario():
        calls = []
        async def handle(req):
            calls.append(json.loads(req.content))
            assert req.url.path == "/api/chat"
            assert req.extensions["timeout"]["read"] == 2
            return httpx.Response(200, json=response_body)
        adapter = OllamaAdapter(base_url="http://127.0.0.1:11434",
            async_client_factory=lambda seconds: httpx.AsyncClient(
                transport=httpx.MockTransport(handle), timeout=seconds))
        try:
            if response_body == {"done": True, "done_reason": "load"}:
                await adapter.prepare_model(request())
            else:
                with pytest.raises(InferenceError) as failure:
                    await adapter.prepare_model(request())
                assert failure.value.category is InferenceErrorCategory.INVALID_RESPONSE
            assert calls == [{"model": "gemma4:e4b", "messages": [], "stream": False,
                "think": False, "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 1024}}]
        finally:
            adapter.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("status,category", [
    (404, InferenceErrorCategory.MODEL_NOT_FOUND),
    (503, InferenceErrorCategory.UNAVAILABLE),
])
def test_preload_http_failures_are_normalized_without_retry(status, category):
    async def scenario():
        calls = []
        async def handle(req):
            calls.append(req)
            return httpx.Response(status, json={"error": "PRIVATE_SENTINEL"})
        adapter = OllamaAdapter(base_url="http://127.0.0.1:11434",
            async_client_factory=lambda seconds: httpx.AsyncClient(
                transport=httpx.MockTransport(handle), timeout=seconds))
        try:
            with pytest.raises(InferenceError) as failure:
                await adapter.prepare_model(request())
            assert failure.value.category is category
            assert "PRIVATE_SENTINEL" not in str(failure.value)
            assert len(calls) == 1
        finally:
            adapter.close()
    asyncio.run(scenario())


class PreparingAdapter(_FakeAdapter):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def prepare_model(self, request):
        self.requests.append(request)
        self.entered.set()
        await self.release.wait()


def test_preparation_uses_target_settings_and_records_a_separate_operation():
    async def scenario():
        adapter = PreparingAdapter()
        observations = []
        router = _router(adapter, observer=observations.append)
        adapter.release.set()
        assert await router.prepare_text(**CHAT, latency_sensitive=True)
        assert len(adapter.requests) == 1
        req = adapter.requests[0]
        assert (req.model_id, req.max_input_tokens, req.max_output_tokens) == ("gemma4:e4b", 8192, 1024)
        assert req.latency_sensitive is True
        assert not hasattr(req, "messages")
        assert observations[0].capability is InferenceCapability.PREPARE_MODEL
        assert observations[0].external_request_count == 1
        assert adapter.text_calls == adapter.estimate_calls == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["queued", "running"])
def test_preparation_cancellation_releases_only_its_own_capacity(phase):
    async def scenario():
        adapter = PreparingAdapter()
        observations = []
        router = _router(adapter, observer=observations.append)
        capacity = router._capacity[InferenceTarget.CHAT] = BoundedSemaphore(1)
        if phase == "queued":
            assert capacity.acquire(blocking=False)
        pending = asyncio.create_task(router.prepare_text(**CHAT))
        if phase == "queued":
            await asyncio.sleep(0.02)
            assert not adapter.requests
        else:
            await adapter.entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        if phase == "queued":
            assert not capacity.acquire(blocking=False)
            capacity.release()
        assert capacity.acquire(blocking=False)
        capacity.release()
        assert observations[0].error_category is InferenceErrorCategory.CANCELLED
        assert observations[0].external_request_count == int(phase == "running")
        adapter.release.set()
        assert await router.prepare_text(**CHAT)
    asyncio.run(scenario())


def test_preparation_queue_timeout_is_not_an_unbounded_wait():
    async def scenario():
        adapter = PreparingAdapter()
        router = _router(adapter)
        resolved = router._settings.targets[InferenceTarget.CHAT]
        router._settings = replace(router._settings, targets={**router._settings.targets,
            InferenceTarget.CHAT: replace(resolved, timeout_seconds=0.02)})
        capacity = router._capacity[InferenceTarget.CHAT] = BoundedSemaphore(1)
        assert capacity.acquire(blocking=False)
        try:
            with pytest.raises(InferenceError) as failure:
                await router.prepare_text(**CHAT)
            assert failure.value.category is InferenceErrorCategory.TIMEOUT
            assert not adapter.requests
            assert not capacity.acquire(blocking=False)
        finally:
            capacity.release()
    asyncio.run(scenario())


def test_preparation_cannot_bypass_target_authorization():
    async def scenario():
        adapter = PreparingAdapter()
        router = _router(adapter)
        with pytest.raises(InferenceError) as failure:
            await router.prepare_text(caller=InferenceCaller.CHAT, target=InferenceTarget.PRIVACY)
        assert failure.value.category is InferenceErrorCategory.ACCESS_DENIED
        assert not adapter.requests
    asyncio.run(scenario())


@pytest.mark.parametrize("failure_stage", [None, "stt", "inference", "prompt", "cancel", "unsupported"])
def test_core_factory_requires_stt_and_model_before_opening_history(monkeypatch, failure_stage):
    from app.livekit_transport import production
    from app.characters.loader import VoicevoxTtsConfig
    async def scenario():
        calls = []
        entered, release = asyncio.Event(), asyncio.Event()
        def transcribe(audio):
            calls.append("stt")
            assert audio == bytes(3200)
            if failure_stage == "stt":
                raise RemoteWhisperTimeoutError("PRIVATE_SENTINEL")
            return "無音の認識結果を会話に保存してはいけない"
        async def prepare_model():
            calls.append("inference")
            entered.set()
            if failure_stage == "inference":
                raise InferenceError(InferenceErrorCategory.TIMEOUT, retryable=True)
            await release.wait()
            return failure_stage != "unsupported"
        async def prepare_prompt(character):
            assert character == "miori"
            calls.append("prompt")
            if failure_stage == "prompt":
                raise InferenceError(InferenceErrorCategory.TIMEOUT, retryable=True)
        def open_history(*_args):
            calls.append("history")
            return object()
        monkeypatch.setattr(production, "load_tts_config", lambda _: VoicevoxTtsConfig(speaker_id=14))
        factory = production.ProductionConversationCoreSessionFactory(
            transcriber=SimpleNamespace(transcribe=transcribe), synthesizer=object(),
            history_service=SimpleNamespace(open_session=open_history),
            generate_reply=lambda *_: pytest.fail("準備では応答を生成しない"),
            prepare_inference=prepare_model, prepare_prompt=prepare_prompt,
        )
        pending = asyncio.create_task(factory.create_ready(
            session_id=str(uuid4()), character_id="miori", conversation_id=uuid4(), delivery=object()))
        if failure_stage == "prompt":
            release.set()
        if failure_stage in {"stt", "inference", "prompt"}:
            with pytest.raises(VoiceModelPreparationError) as failure:
                await pending
            assert failure.value.stage == ("inference" if failure_stage == "prompt" else failure_stage)
            assert failure.value.code == ("stt_inference_timeout" if failure_stage == "stt" else "inference_timeout")
            assert "PRIVATE_SENTINEL" not in str(failure.value)
            assert "history" not in calls
        else:
            await asyncio.wait_for(entered.wait(), 1)
            assert calls == ["stt", "inference"]
            assert not pending.done()
            if failure_stage == "cancel":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert "history" not in calls
            else:
                release.set()
                session = await pending
                assert calls == (["stt", "inference", "history"] if failure_stage == "unsupported"
                                 else ["stt", "inference", "prompt", "history"])
                await session.end()
    asyncio.run(scenario())


def test_provider_without_preload_does_not_generate_a_dummy_conversation():
    from app.inference.config import resolve_inference_settings
    from app.inference.registry import default_provider_registry
    from app.inference.router import InferenceRouter
    from tests.unit.test_inference_core import _environment
    class CloudAdapter(_FakeAdapter):
        provider_id = "openai-api"
        capabilities = frozenset(InferenceCapability) - {InferenceCapability.PREPARE_MODEL}
    async def scenario():
        adapter = CloudAdapter()
        registry = default_provider_registry()
        registry.bind(adapter)
        env = _environment()
        env["INFERENCE_TARGET_CHAT"] = "openai-api/configured-model"
        router = InferenceRouter(settings=resolve_inference_settings(env, registry), registry=registry)
        assert not await router.prepare_text(**CHAT)
        assert adapter.text_calls == adapter.estimate_calls == 0
    asyncio.run(scenario())

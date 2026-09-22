"""共有サービスを起動・停止せず、本文なしの開始準備と後続処理を実接続で検証する。"""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from app.conversation_core.adapters import WhisperSttAdapter
from app.inference.authorization import InferenceCaller
from app.inference.contracts import InferenceCapability, InferenceMessage, InferenceTarget
from app.inference.runtime import create_inference_runtime
from app.stt.remote_whisper_client import RemoteWhisperTranscriber


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_VOICE_MODEL_PREPARATION_TESTS") != "1",
    reason="実Whisper/Ollamaへの開始準備試験を明示した場合のみ実行",
)


def test_real_ollama_preload_uses_chat_context_and_allows_following_generation():
    async def scenario():
        runtime = create_inference_runtime(os.environ)
        observations = []
        runtime.router._observer = observations.append
        target = runtime.settings.target(InferenceTarget.CHAT)
        try:
            assert target.reference.provider_id == "ollama"
            assert await runtime.router.prepare_text(
                caller=InferenceCaller.CHAT, target=InferenceTarget.CHAT, latency_sensitive=True)
            # 準備成功の直後に、常駐contextを公開APIで照合する。要求以外の設定を変更しない。
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                result = await client.get(os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/ps")
                result.raise_for_status()
                models = result.json()["models"]
            loaded = [model for model in models if model.get("name") == target.reference.model_id]
            assert loaded
            assert loaded[0]["context_length"] == target.max_input_tokens + target.max_output_tokens
            assert observations[-1].capability is InferenceCapability.PREPARE_MODEL
            assert observations[-1].success
            chunks = [chunk async for chunk in runtime.router.stream_text(
                caller=InferenceCaller.CHAT, target=InferenceTarget.CHAT,
                messages=(InferenceMessage("user", "動作確認です。短く挨拶してください。"),),
                latency_sensitive=True)]
            assert "".join(chunks).strip()
        finally:
            runtime.close()
    asyncio.run(scenario())


def test_real_whisper_required_preparation_and_following_transcription():
    async def scenario():
        client = RemoteWhisperTranscriber(os.environ.get("WHISPER_BASE_URL", "http://127.0.0.1:50022"))
        adapter = WhisperSttAdapter(transcriber=client)
        try:
            await adapter.prepare_required()
            # 同じ枠が準備後に戻り、後続の通常STT要求を処理できることを確認する。
            assert isinstance(await adapter.transcribe(bytes(3200)), str)
        finally:
            client.close()
    asyncio.run(scenario())

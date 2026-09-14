from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.characters.loader import IrodoriTtsConfig, TtsConfigValidationError, load_tts_config
from app.tts.irodori_client import IrodoriClient, IrodoriRuntimeConfig, IrodoriTtsError
from tests.character_card_test_support import (
    character_card_data, character_card_document, use_character_repo_root, write_character_card,
)
from tests.unit.test_irodori_service import wav_bytes


def voice() -> IrodoriTtsConfig:
    assets = Path(__file__).resolve().parents[3] / "characters/miori/assets/voice"
    record = json.loads((assets / "miori-b3-4221.json").read_text())
    request = record["reference_synthesis_request"]
    return IrodoriTtsConfig(
        voice_id=record["voice_id"], speed=request["speed"], **request["irodori"],
    )


def test_selected_voice_loads_from_ccv_without_server_paths(tmp_path, monkeypatch) -> None:
    data = character_card_data(extensions={"digital_souls": {"tts_config": voice().model_dump()}})
    write_character_card(tmp_path, "test", character_card_document(data=data))
    use_character_repo_root(monkeypatch, tmp_path)
    assert load_tts_config("test") == voice()
    data["extensions"]["digital_souls"]["tts_config"]["base_url"] = "http://example.test"
    (tmp_path / "characters/test/test.card.json").write_text(
        json.dumps(character_card_document(data=data)),
    )
    with pytest.raises(TtsConfigValidationError):
        load_tts_config("test")


@pytest.mark.parametrize("changes", [
    {"voice_id": "none"}, {"voice_id": "../voice"}, {"seed": True},
    {"num_steps": 0}, {"speed": float("nan")}, {"speaker_id": 14},
])
def test_invalid_irodori_ccv_is_rejected(changes) -> None:
    with pytest.raises(ValueError):
        IrodoriTtsConfig.model_validate(voice().model_dump() | changes)


def test_client_sends_selected_settings_and_normalizes_only_valid_wav() -> None:
    requests = []
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ready"})
        if request.url.path == "/v1/audio/voices":
            return httpx.Response(200, json={"data": [{"id": voice().voice_id}]})
        return httpx.Response(200, content=wav_bytes(), headers={"content-type": "audio/wav"})

    async def scenario() -> None:
        client = IrodoriClient(IrodoriRuntimeConfig(environment="test"), transport=httpx.MockTransport(handle))
        await client.ensure_ready(voice())
        assert await client.synthesize_pcm("最初の区間。", voice()) == b"\x10\x00" * 480
        body = json.loads(requests[-1].content)
        assert body["input"] == "最初の区間。"
        assert body["voice"] == "miori-b3-4221"
        assert body["irodori"] == {
            "caption": voice().caption, "seed": 4221, "num_steps": 40, "chunking_enabled": False,
        }
        assert requests[-1].headers["X-DS-Environment"] == "test"
    asyncio.run(scenario())


@pytest.mark.parametrize("status,body,content_type,code", [
    (200, b"not a wav", "audio/wav", "tts_invalid_audio"),
    (200, wav_bytes()[:-2], "audio/wav", "tts_invalid_audio"),
    (200, wav_bytes(), "application/json", "tts_invalid_audio"),
    (429, b'{"error":{"code":"tts_capacity_exceeded"}}', "application/json", "tts_capacity_exceeded"),
    (500, b"secret native error", "text/plain", "tts_service_failed"),
])
def test_invalid_or_failed_audio_is_never_emitted(status, body, content_type, code) -> None:
    async def scenario() -> None:
        client = IrodoriClient(
            IrodoriRuntimeConfig(),
            transport=httpx.MockTransport(lambda _: httpx.Response(
                status, content=body, headers={"content-type": content_type},
            )),
        )
        with pytest.raises(IrodoriTtsError, match=code) as failure:
            await client.synthesize_pcm("保存しない本文", voice())
        assert str(failure.value) == code
    asyncio.run(scenario())


def test_cancel_closes_http_wait_without_retry_or_fallback() -> None:
    async def scenario() -> None:
        entered, cancelled = asyncio.Event(), asyncio.Event()
        calls = []
        async def handle(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("unreachable")
        client = IrodoriClient(IrodoriRuntimeConfig(), transport=httpx.MockTransport(handle))
        pending = asyncio.create_task(client.synthesize_pcm("旧応答。", voice()))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert cancelled.is_set() and len(calls) == 1
    asyncio.run(scenario())


def test_factory_waits_for_readiness_and_freezes_voice_until_new_session(monkeypatch) -> None:
    from app.livekit_transport import production
    async def scenario() -> None:
        selected = voice()
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        class Client:
            async def ensure_ready(self, config) -> None:
                seen.append(config.voice_id)
                entered.set()
                await release.wait()
            async def synthesize_pcm(self, text, config):
                seen.append(config.voice_id)
                return b"\x10\x00" * 480
        class Delivery:
            async def publish(self, event): pass
        class History:
            def open_session(self, *args): return object()
        monkeypatch.setattr(production, "load_tts_config", lambda _: selected)
        factory = production.ProductionConversationCoreSessionFactory(
            transcriber=object(), synthesizer=object(), history_service=History(),
            irodori_client=Client(), generate_reply=lambda *_: "応答。",
        )
        args = dict(session_id=str(uuid4()), character_id="miori", conversation_id=uuid4(), delivery=Delivery())
        pending = asyncio.create_task(factory.create_ready(**args))
        await entered.wait()
        assert not pending.done()
        selected = selected.model_copy(update={"voice_id": "new-voice"})
        release.set()
        session = await pending
        async for _ in session._tts.synthesize("設定変更前の声。"): pass
        # 一時再接続はCoreを作り直さず、保持しているadapterをそのまま使う。
        await session.reconnect()
        async for _ in session._tts.synthesize("再接続後の声。"): pass
        next_session = await factory.create_ready(**(args | {"session_id": str(uuid4())}))
        async for _ in next_session._tts.synthesize("新しい会話。"): pass
        assert seen == ["miori-b3-4221"] * 3 + ["new-voice"] * 2
        await session.end()
        await next_session.end()
    asyncio.run(scenario())


def test_unready_service_never_creates_a_conversation(monkeypatch) -> None:
    from app.livekit_transport import production
    async def scenario() -> None:
        async def reject(_): raise IrodoriTtsError("tts_not_ready")
        monkeypatch.setattr(production, "load_tts_config", lambda _: voice())
        factory = production.ProductionConversationCoreSessionFactory(
            transcriber=object(), synthesizer=object(),
            history_service=SimpleNamespace(open_session=lambda *_: pytest.fail("must not open")),
            irodori_client=SimpleNamespace(ensure_ready=reject),
            generate_reply=lambda *_: "応答。",
        )
        with pytest.raises(IrodoriTtsError, match="tts_not_ready"):
            await factory.create_ready(
                session_id=str(uuid4()), character_id="miori",
                conversation_id=uuid4(), delivery=object(),
            )
    asyncio.run(scenario())

@pytest.mark.parametrize("fail_first", [False, True])
def test_real_adapter_keeps_segment_streaming_and_allows_next_response_after_failure(fail_first) -> None:
    from app.conversation_core import ConversationCoreSession, TextDelta
    from app.tts.irodori_client import IrodoriTtsAdapter
    from tests.conversation_core_test_support import (
        RecordingDelivery, RecordingObservation, RecordingPersistence, RecordingStt, event_field,
    )
    async def scenario() -> None:
        first_sent, rest_allowed, llm_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        generation_count = 0
        class Llm:
            async def generate(self, _):
                nonlocal generation_count
                generation_count += 1
                try:
                    yield TextDelta(1, "光織の声です。", (0, 7))
                    if generation_count == 1:
                        await rest_allowed.wait()
                    yield TextDelta(2, "続きです。", (7, 12))
                finally:
                    llm_closed.set()
        def handle(request):
            calls.append(json.loads(request.content)["input"])
            first_sent.set()
            if fail_first and len(calls) == 1:
                return httpx.Response(504, json={"error": {"code": "tts_inference_timeout"}})
            return httpx.Response(200, content=wav_bytes(), headers={"content-type": "audio/wav"})
        delivery = RecordingDelivery()
        session = ConversationCoreSession(
            session_id=str(uuid4()), response_id_factory=lambda: str(uuid4()),
            delivery=delivery, persistence=RecordingPersistence(),
            observation=RecordingObservation(), stt=RecordingStt(), llm=Llm(),
            tts=IrodoriTtsAdapter(
                client=IrodoriClient(IrodoriRuntimeConfig(), transport=httpx.MockTransport(handle)),
                voice=voice(),
            ),
        )
        async def settle():
            while session.running_stage_count:
                await asyncio.sleep(0)
        first = await session.finalize_utterance(
            utterance_id=str(uuid4()), transcript="話して", should_response=True,
        )
        await asyncio.wait_for(first_sent.wait(), 1)
        if fail_first:
            await asyncio.wait_for(settle(), 1)
            assert session.response(first.response_id).state.value == "failed"
            assert llm_closed.is_set()
            assert calls == ["みおりの声です。"]
            assert not any(event_field(e, "type") == "response_audio_segment" for e in delivery.events)
            second = await session.finalize_utterance(
                utterance_id=str(uuid4()), transcript="もう一度", should_response=True,
            )
            await asyncio.wait_for(settle(), 1)
            assert session.response(second.response_id).state.value == "completed"
        else:
            async def wait_audio():
                while not any(event_field(e, "type") == "response_audio_segment" for e in delivery.events):
                    await asyncio.sleep(0)
            await asyncio.wait_for(wait_audio(), 1)
            assert not rest_allowed.is_set()
            assert calls == ["みおりの声です。"]
            rest_allowed.set()
            await asyncio.wait_for(settle(), 1)
            audio = [e for e in delivery.events if event_field(e, "type") == "response_audio_segment"]
            assert [event_field(e, "audio_sequence") for e in audio] == [1, 2]
            assert [event_field(e, "text_range") for e in audio] == [(0, 7), (7, 12)]
        await session.end()
    asyncio.run(scenario())


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:50024", "http://127.0.0.2:50024",
    "http://[::1]:50024", "http://localhost:50024", "https://tts.example.test",
])
def test_dogfood_accepts_loopback_http_or_remote_https(url) -> None:
    assert IrodoriRuntimeConfig(base_url=url, environment="dogfood").base_url == url


@pytest.mark.parametrize("url", ["http://tts.example.test", "http://192.168.1.10:50024"])
def test_dogfood_rejects_cleartext_remote_tts(url) -> None:
    with pytest.raises(ValueError, match="HTTPS or a loopback host"):
        IrodoriRuntimeConfig(base_url=url, environment="dogfood")


def test_tts_redirect_does_not_forward_private_text() -> None:
    async def scenario() -> None:
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(307, headers={"location": "https://untrusted.example.test"})
        client = IrodoriClient(IrodoriRuntimeConfig(), transport=httpx.MockTransport(handle))
        with pytest.raises(IrodoriTtsError):
            await client.synthesize_pcm("転送しない本文。", voice())
        assert len(calls) == 1
    asyncio.run(scenario())

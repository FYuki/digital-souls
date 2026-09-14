from __future__ import annotations

import asyncio
import io
import ipaddress
import math
import os
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.characters.loader import IrodoriTtsConfig
from app.conversation_core.models import AudioSegment
from app.tts.speech_synthesizer import SpeechSynthesisError


MAX_AUDIO_BYTES = 48_000 * 2 * 60 + 4096
_SPEECH_READINGS = (("光織", "みおり"),)
_ERROR_CODES = frozenset({
    "tts_not_ready", "tts_capacity_exceeded", "tts_queue_timeout",
    "tts_inference_timeout", "tts_inference_failed", "tts_worker_failed",
    "tts_invalid_audio", "tts_voice_missing", "tts_voice_not_found", "tts_voice_changed",
    "tts_voice_invalid", "tts_invalid_request", "tts_request_too_large",
})


class IrodoriTtsError(SpeechSynthesisError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class IrodoriRuntimeConfig:
    base_url: str = "http://127.0.0.1:50024"
    request_timeout: float = 45.0
    readiness_timeout: float = 5.0
    environment: str = "dev"

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
        ):
            raise ValueError("IRODORI_BASE_URL must be an HTTP service origin")
        if self.environment == "dogfood" and parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
            except ValueError:
                loopback = parsed.hostname == "localhost"
            if not loopback:
                raise ValueError("dogfood IRODORI_BASE_URL requires HTTPS or a loopback host")
        if self.environment not in {"dev", "test", "dogfood"}:
            raise ValueError("DS_ENVIRONMENT_ID must be dev, test or dogfood")
        if any(not math.isfinite(value) or value <= 0 for value in (
            self.request_timeout, self.readiness_timeout,
        )):
            raise ValueError("Irodori timeouts must be positive and finite")

    @classmethod
    def from_env(cls) -> IrodoriRuntimeConfig:
        return cls(
            base_url=os.environ.get("IRODORI_BASE_URL", "http://127.0.0.1:50024").rstrip("/"),
            request_timeout=float(os.environ.get("IRODORI_REQUEST_TIMEOUT_SECONDS", "45")),
            readiness_timeout=float(os.environ.get("IRODORI_READINESS_TIMEOUT_SECONDS", "5")),
            environment=os.environ.get("DS_ENVIRONMENT_ID", "dev"),
        )


class IrodoriClient:
    """共有サービスの所有権を持たない、取消可能なHTTPクライアント。"""

    def __init__(
        self, config: IrodoriRuntimeConfig, *, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.base_url, timeout=timeout, transport=self._transport,
            follow_redirects=False, trust_env=False,
            headers={"X-DS-Environment": self.config.environment},
        )

    async def ensure_ready(self, voice: IrodoriTtsConfig) -> None:
        try:
            await asyncio.wait_for(self._check_ready(voice), self.config.readiness_timeout)
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as error:
            raise IrodoriTtsError("tts_not_ready") from error

    async def _check_ready(self, voice: IrodoriTtsConfig) -> None:
        async with self._client(self.config.readiness_timeout) as client:
            ready = await client.get("/health/ready")
            if ready.status_code != 200 or ready.json() != {"status": "ready"}:
                raise IrodoriTtsError("tts_not_ready")
            voices = await client.get("/v1/audio/voices")
            if voices.status_code != 200:
                raise IrodoriTtsError("tts_not_ready")
            if not any(item.get("id") == voice.voice_id for item in voices.json()["data"]):
                raise IrodoriTtsError("tts_voice_missing")

    async def synthesize_pcm(self, text: str, voice: IrodoriTtsConfig) -> bytes:
        try:
            return await asyncio.wait_for(
                self._synthesize_pcm(text, voice), self.config.request_timeout,
            )
        except (httpx.TimeoutException, asyncio.TimeoutError) as error:
            raise IrodoriTtsError("tts_request_timeout") from error
        except httpx.HTTPError as error:
            raise IrodoriTtsError("tts_service_unavailable") from error

    async def _synthesize_pcm(self, text: str, voice: IrodoriTtsConfig) -> bytes:
        # 表示・履歴・text_rangeの原文を保ち、合成へ渡す文字列だけ読みを適用する。
        speech_text = text
        for written, reading in _SPEECH_READINGS:
            speech_text = speech_text.replace(written, reading)
        payload = {
            "model": "irodori-tts", "input": speech_text, "voice": voice.voice_id,
            "response_format": "wav", "speed": voice.speed,
            "irodori": {
                "caption": voice.caption, "seed": voice.seed,
                "num_steps": voice.num_steps, "chunking_enabled": False,
            },
        }
        async with self._client(self.config.request_timeout) as client:
            async with client.stream("POST", "/v1/audio/speech", json=payload) as response:
                if response.status_code != 200:
                    # native error本文を例外・ログへ転記しない。
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 4096:
                            raise IrodoriTtsError("tts_service_failed")
                    try:
                        import json
                        code = json.loads(body)["error"]["code"]
                    except (ValueError, KeyError, TypeError):
                        code = None
                    raise IrodoriTtsError(
                        code if isinstance(code, str) and code in _ERROR_CODES else "tts_service_failed"
                    )
                if response.headers.get("content-type", "").split(";")[0] != "audio/wav":
                    raise IrodoriTtsError("tts_invalid_audio")
                audio = bytearray()
                async for chunk in response.aiter_bytes():
                    audio.extend(chunk)
                    if len(audio) > MAX_AUDIO_BYTES:
                        raise IrodoriTtsError("tts_invalid_audio")
        try:
            with wave.open(io.BytesIO(audio), "rb") as wav:
                frames = wav.getnframes()
                if (
                    wav.getcomptype() != "NONE" or wav.getnchannels() != 1
                    or wav.getsampwidth() != 2 or wav.getframerate() != 48_000 or frames < 1
                ):
                    raise IrodoriTtsError("tts_invalid_audio")
                pcm = wav.readframes(frames)
                if len(pcm) != frames * 2 or not any(pcm):
                    raise IrodoriTtsError("tts_invalid_audio")
                return pcm
        except (wave.Error, EOFError, OSError) as error:
            raise IrodoriTtsError("tts_invalid_audio") from error


class IrodoriTtsAdapter:
    def __init__(self, *, client: IrodoriClient, voice: IrodoriTtsConfig) -> None:
        self._client = client
        self._voice = voice

    async def prepare(self) -> None:
        await self._client.ensure_ready(self._voice)

    async def synthesize(self, text: str) -> AsyncIterator[AudioSegment]:
        pcm = await self._client.synthesize_pcm(text, self._voice)
        yield AudioSegment(audio_sequence=1, audio=pcm, text_range=(0, len(text)))

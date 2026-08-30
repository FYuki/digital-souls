from __future__ import annotations

import httpx


WHISPER_BASE_URL_ENV = "WHISPER_BASE_URL"
DEFAULT_WHISPER_BASE_URL = "http://127.0.0.1:50022"
DEFAULT_WHISPER_TIMEOUT_SECONDS = 50.0
WHISPER_DEVICE = "cuda"
WHISPER_COMPUTE_TYPE = "int8_float16"


class RemoteWhisperError(RuntimeError):
    error_code = "stt_upstream_failed"


class RemoteWhisperCapacityError(RemoteWhisperError):
    error_code = "stt_capacity_exceeded"


class RemoteWhisperTimeoutError(RemoteWhisperError):
    error_code = "stt_inference_timeout"


class RemoteWhisperTranscriber:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = DEFAULT_WHISPER_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 45:
            raise ValueError("Backend Whisper timeout must exceed service inference timeout")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    def transcribe(self, audio: bytes) -> str:
        try:
            response = self._client.post(
                "/v1/transcriptions",
                content=audio,
                headers={"Content-Type": "application/octet-stream"},
            )
        except httpx.TimeoutException as error:
            raise RemoteWhisperTimeoutError("Whisper request timed out") from error
        except httpx.HTTPError as error:
            raise RemoteWhisperError("Whisper request failed") from error
        if response.status_code == 429:
            raise RemoteWhisperCapacityError("Whisper capacity exceeded")
        if response.status_code == 504:
            raise RemoteWhisperTimeoutError("Whisper inference timed out")
        if response.status_code != 200:
            raise RemoteWhisperError(f"Whisper returned HTTP {response.status_code}")
        try:
            payload = response.json()
            transcript = payload["text"]
        except (ValueError, KeyError, TypeError) as error:
            raise RemoteWhisperError("Whisper response is invalid") from error
        if not isinstance(transcript, str):
            raise RemoteWhisperError("Whisper response text is invalid")
        return transcript

    def close(self) -> None:
        self._client.close()

from __future__ import annotations

import httpx
import pytest

from app.stt.remote_whisper_client import (
    RemoteWhisperCapacityError,
    RemoteWhisperError,
    RemoteWhisperTimeoutError,
    RemoteWhisperTranscriber,
)


def _client(handler) -> RemoteWhisperTranscriber:
    return RemoteWhisperTranscriber(
        "http://127.0.0.1:50022",
        transport=httpx.MockTransport(handler),
    )


def test_should_send_fixed_pcm_contract_and_return_transcript() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/transcriptions"
        assert request.headers["content-type"] == "application/octet-stream"
        assert request.content == b"\x00\x00"
        return httpx.Response(200, json={"text": "こんにちは"})

    client = _client(handler)
    try:
        assert client.transcribe(b"\x00\x00") == "こんにちは"
    finally:
        client.close()


@pytest.mark.parametrize(
    ("status", "error_type", "error_code"),
    (
        (429, RemoteWhisperCapacityError, "stt_capacity_exceeded"),
        (504, RemoteWhisperTimeoutError, "stt_inference_timeout"),
        (503, RemoteWhisperError, "stt_upstream_failed"),
    ),
)
def test_should_map_remote_failures(status, error_type, error_code) -> None:
    client = _client(lambda _request: httpx.Response(status, json={"error": {"code": error_code}}))
    try:
        with pytest.raises(error_type) as caught:
            client.transcribe(b"\x00\x00")
        assert caught.value.error_code == error_code
    finally:
        client.close()


def test_should_reject_timeout_not_longer_than_service_timeout() -> None:
    with pytest.raises(ValueError, match="must exceed"):
        RemoteWhisperTranscriber("http://127.0.0.1:50022", timeout_seconds=45)

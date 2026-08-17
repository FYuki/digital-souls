from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest


_PATCH_POST = "app.privacy.semantic.ollama_classifier_client.httpx.post"
_DIGEST_HEX = "c" * 64
_PRIVATE_SENTINELS = (
    "private-raw-text-sentinel",
    "private-text-hash-sentinel",
    "private-model-output-sentinel",
    "private-parser-frame-sentinel",
)


def _response(body: object) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = body
    return response


def test_chat_uses_json_format_local_endpoint_and_per_call_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.privacy.semantic.ollama_classifier_client import OllamaClassifierClient

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://local-ollama:11434")
    response = _response({"message": {"content": "{}"}})
    messages = ({"role": "user", "content": "synthetic input"},)

    with patch(_PATCH_POST, return_value=response) as post:
        result = OllamaClassifierClient(model_id="gemma4:e4b").chat(
            messages, timeout_seconds=2.0
        )

    assert result == "{}"
    assert post.call_args.args[0] == "http://local-ollama:11434/api/chat"
    assert post.call_args.kwargs["json"] == {
        "model": "gemma4:e4b",
        "stream": False,
        "format": "json",
        "messages": list(messages),
    }
    timeout = post.call_args.kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 2.0


def test_model_digest_is_resolved_once_and_cached() -> None:
    from app.privacy.semantic.ollama_classifier_client import OllamaClassifierClient

    response = _response(
        {"modelfile": f"FROM /models/blobs/sha256-{_DIGEST_HEX}\nTEMPLATE test"}
    )
    with patch(_PATCH_POST, return_value=response) as post:
        client = OllamaClassifierClient(model_id="gemma4:e4b")
        first = client.resolve_model_digest()
        second = client.resolve_model_digest()

    assert first == second == f"sha256:{_DIGEST_HEX}"
    post.assert_called_once()
    assert post.call_args.args[0].endswith("/api/show")
    assert post.call_args.kwargs["json"] == {"model": "gemma4:e4b"}


def test_http_error_does_not_expose_response_body_or_prompt() -> None:
    from app.privacy.semantic.ollama_classifier_client import OllamaClassifierClient

    request = httpx.Request("POST", "http://localhost:11434/api/chat")
    raw_body = "raw response containing private material"
    response = _response({"error": raw_body})
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "status failure",
        request=request,
        response=httpx.Response(500, request=request, text=raw_body),
    )
    prompt = "synthetic-private-prompt"

    with patch(_PATCH_POST, return_value=response):
        with pytest.raises(Exception) as exc_info:
            OllamaClassifierClient(model_id="gemma4:e4b").chat(
                ({"role": "user", "content": prompt},), timeout_seconds=2.0
            )

    assert raw_body not in str(exc_info.value)
    assert prompt not in str(exc_info.value)


def test_invalid_json_does_not_expose_private_values_in_exception_or_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.privacy.semantic.ollama_classifier_client import (
        OllamaClassifierClient,
        OllamaInvalidResponseError,
    )

    caplog.set_level(logging.DEBUG)
    response = _response(None)
    response.json.side_effect = ValueError(" ".join(_PRIVATE_SENTINELS))

    with patch(_PATCH_POST, return_value=response):
        with pytest.raises(OllamaInvalidResponseError) as exc_info:
            OllamaClassifierClient(model_id="gemma4:e4b").chat(
                ({"role": "user", "content": _PRIVATE_SENTINELS[0]},),
                timeout_seconds=2.0,
            )

    for sentinel in _PRIVATE_SENTINELS:
        assert sentinel not in str(exc_info.value)
        assert sentinel not in caplog.text


def test_invalid_response_shape_does_not_expose_private_values_in_exception_or_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.privacy.semantic.ollama_classifier_client import (
        OllamaClassifierClient,
        OllamaInvalidResponseError,
    )

    caplog.set_level(logging.DEBUG)
    response = _response({"message": " ".join(_PRIVATE_SENTINELS)})

    with patch(_PATCH_POST, return_value=response):
        with pytest.raises(OllamaInvalidResponseError) as exc_info:
            OllamaClassifierClient(model_id="gemma4:e4b").chat(
                ({"role": "user", "content": _PRIVATE_SENTINELS[0]},),
                timeout_seconds=2.0,
            )

    for sentinel in _PRIVATE_SENTINELS:
        assert sentinel not in str(exc_info.value)
        assert sentinel not in caplog.text

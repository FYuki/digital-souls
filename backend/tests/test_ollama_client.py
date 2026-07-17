from unittest.mock import MagicMock, patch

import httpx

import pytest


def _mock_response(content: str) -> MagicMock:
    mock = MagicMock()
    mock.json.return_value = {
        "model": "gemma4:e4b",
        "message": {"role": "assistant", "content": content},
        "done": True,
    }
    mock.raise_for_status.return_value = None
    return mock


_PATCH_HTTPX_POST = "app.llm.ollama_client.httpx.post"


class TestOllamaClientGenerate:
    def test_sends_post_to_api_chat_path(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate("system", "user")

        called_url: str = mock_post.call_args.args[0]
        assert called_url.endswith("/api/chat")

    def test_uses_default_base_url_when_env_not_set(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate("system", "user")

        called_url: str = mock_post.call_args.args[0]
        assert called_url.startswith("http://localhost:11434")

    def test_uses_custom_base_url_from_env(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://custom-host:9999")

        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate("system", "user")

        called_url: str = mock_post.call_args.args[0]
        assert called_url.startswith("http://custom-host:9999")

    def test_payload_model_is_gemma4_e4b(self):
        from app.llm.ollama_client import OllamaClient
        from app.model_settings import OLLAMA_MODEL_NAME

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate("system", "user")

        payload: dict = mock_post.call_args.kwargs.get("json", {})
        assert payload.get("model") == OLLAMA_MODEL_NAME

    def test_payload_stream_is_false(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate("system", "user")

        payload: dict = mock_post.call_args.kwargs.get("json", {})
        assert payload.get("stream") is False

    def test_payload_messages_has_two_entries(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate("system prompt", "user message")

        payload: dict = mock_post.call_args.kwargs.get("json", {})
        assert len(payload.get("messages", [])) == 2

    def test_payload_first_message_is_system_role(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate("system prompt", "user message")

        messages = mock_post.call_args.kwargs.get("json", {}).get("messages", [])
        assert messages[0]["role"] == "system"

    def test_payload_first_message_content_is_system_prompt(self):
        from app.llm.ollama_client import OllamaClient

        system_prompt = "あなたは光織です。"
        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate(system_prompt, "user message")

        messages = mock_post.call_args.kwargs.get("json", {}).get("messages", [])
        assert messages[0]["content"] == system_prompt

    def test_payload_second_message_is_user_role(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate("system prompt", "user message")

        messages = mock_post.call_args.kwargs.get("json", {}).get("messages", [])
        assert messages[1]["role"] == "user"

    def test_payload_second_message_content_is_user_message(self):
        from app.llm.ollama_client import OllamaClient

        user_message = "こんにちは、自己紹介してください。"
        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate("system", user_message)

        messages = mock_post.call_args.kwargs.get("json", {}).get("messages", [])
        assert messages[1]["content"] == user_message

    def test_returns_message_content_from_ollama_response(self):
        from app.llm.ollama_client import OllamaClient

        expected = "光織です。よろしくお願いします。"
        with patch(_PATCH_HTTPX_POST, return_value=_mock_response(expected)):
            result = OllamaClient().generate("system", "user")

        assert result == expected

    def test_return_type_is_str(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("hello")):
            result = OllamaClient().generate("system", "user")

        assert isinstance(result, str)

    def test_passes_explicit_timeout_to_httpx_post(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate("system", "user")

        timeout = mock_post.call_args.kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read == 30.0

    def test_raises_http_status_error_before_reading_response_body(self):
        from app.llm.ollama_client import OllamaClient

        response = _mock_response("should not be read")
        request = httpx.Request("POST", "http://localhost:11434/api/chat")
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "server error",
            request=request,
            response=httpx.Response(500, request=request),
        )

        with patch(_PATCH_HTTPX_POST, return_value=response):
            with pytest.raises(httpx.HTTPStatusError):
                OllamaClient().generate("system", "user")

        response.json.assert_not_called()

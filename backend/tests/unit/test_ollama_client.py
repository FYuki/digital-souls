from unittest.mock import MagicMock, patch

import httpx
import pytest


def _mock_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "model": "gemma4:e4b",
        "message": {"role": "assistant", "content": content},
        "done": True,
    }
    response.raise_for_status.return_value = None
    return response


def _built_prompt():
    from tests.prompt_test_support import prompt_build_input, prompt_builder

    return prompt_builder().build(prompt_build_input())


_PATCH_HTTPX_POST = "app.llm.ollama_client.httpx.post"


class TestOllamaClientGenerate:
    def test_sends_built_prompt_messages_without_reassembling_them(self):
        from app.llm.ollama_client import OllamaClient

        built_prompt = _built_prompt()
        expected_messages = [
            {"role": message.role.value, "content": message.content}
            for message in built_prompt.messages
        ]

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate(built_prompt)

        payload = mock_post.call_args.kwargs["json"]
        assert payload["messages"] == expected_messages

    def test_sends_post_to_api_chat_path(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate(_built_prompt())

        called_url: str = mock_post.call_args.args[0]
        assert called_url.endswith("/api/chat")

    def test_uses_default_base_url_when_env_not_set(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate(_built_prompt())

        called_url: str = mock_post.call_args.args[0]
        assert called_url.startswith("http://localhost:11434")

    def test_uses_custom_base_url_from_env(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://custom-host:9999")

        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate(_built_prompt())

        called_url: str = mock_post.call_args.args[0]
        assert called_url.startswith("http://custom-host:9999")

    def test_payload_uses_configured_model(self):
        from app.llm.ollama_client import OllamaClient
        from app.model_settings import OLLAMA_MODEL_NAME

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate(_built_prompt())

        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == OLLAMA_MODEL_NAME

    def test_payload_disables_streaming(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate(_built_prompt())

        payload = mock_post.call_args.kwargs["json"]
        assert payload["stream"] is False

    def test_returns_message_content_from_ollama_response(self):
        from app.llm.ollama_client import OllamaClient

        expected = "光織です。よろしくお願いします。"
        with patch(_PATCH_HTTPX_POST, return_value=_mock_response(expected)):
            result = OllamaClient().generate(_built_prompt())

        assert result == expected

    def test_passes_explicit_timeout_to_httpx_post(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient().generate(_built_prompt())

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
                OllamaClient().generate(_built_prompt())

        response.json.assert_not_called()

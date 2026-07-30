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
    from app.prompting.types import BuiltPrompt, PromptMessage, PromptTokenBudget

    return BuiltPrompt(
        messages=(
            PromptMessage(role="system", content="system one"),
            PromptMessage(role="user", content="old user"),
            PromptMessage(role="assistant", content="old assistant"),
            PromptMessage(role="user", content="current user"),
            PromptMessage(role="system", content="final instruction"),
        ),
        token_budget=PromptTokenBudget(
            character_and_system=100,
            rag=50,
            history=75,
            current_user=40,
            final_instructions=20,
            total=250,
        ),
    )


_PATCH_HTTPX_POST = "app.llm.ollama_client.httpx.post"


class TestOllamaClientGenerate:
    def test_sends_post_to_api_chat_path(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as post:
            OllamaClient().generate(_built_prompt())

        assert post.call_args.args[0].endswith("/api/chat")

    def test_uses_custom_base_url_from_env(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://custom-host:9999")
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as post:
            OllamaClient().generate(_built_prompt())

        assert post.call_args.args[0].startswith("http://custom-host:9999")

    def test_payload_preserves_built_prompt_messages_exactly(self):
        from app.llm.ollama_client import OllamaClient

        prompt = _built_prompt()
        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as post:
            OllamaClient().generate(prompt)

        payload = post.call_args.kwargs["json"]
        assert payload["messages"] == [
            {"role": message.role, "content": message.content}
            for message in prompt.messages
        ]

    def test_payload_uses_configured_model_and_non_streaming_mode(self):
        from app.llm.ollama_client import OllamaClient
        from app.model_settings import OLLAMA_MODEL_NAME

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as post:
            OllamaClient().generate(_built_prompt())

        payload = post.call_args.kwargs["json"]
        assert payload["model"] == OLLAMA_MODEL_NAME
        assert payload["stream"] is False

    def test_returns_message_content_from_response(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("光織の応答")):
            result = OllamaClient().generate(_built_prompt())

        assert result == "光織の応答"

    def test_passes_explicit_timeout(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as post:
            OllamaClient().generate(_built_prompt())

        timeout = post.call_args.kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read == 30.0

    def test_raises_status_error_before_reading_body(self):
        from app.llm.ollama_client import OllamaClient

        response = _mock_response("must not be read")
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

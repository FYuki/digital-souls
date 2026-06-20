import pytest
from unittest.mock import patch


class TestGenerateResponse:
    def test_returns_ollama_client_output(self):
        from app.llm.router import generate_response

        expected = "光織のLLM応答"
        with patch("app.llm.ollama_client.OllamaClient.generate", return_value=expected):
            result = generate_response("system prompt", "user message")

        assert result == expected

    def test_return_type_is_str(self):
        from app.llm.router import generate_response

        with patch("app.llm.ollama_client.OllamaClient.generate", return_value="text"):
            result = generate_response("system", "user")

        assert isinstance(result, str)

    def test_passes_system_prompt_to_client(self):
        from app.llm.router import generate_response

        system_prompt = "あなたは光織です。"
        with patch("app.llm.ollama_client.OllamaClient.generate", return_value="ok") as mock_gen:
            generate_response(system_prompt, "user message")

        args, kwargs = mock_gen.call_args
        all_args = list(args) + list(kwargs.values())
        assert system_prompt in all_args

    def test_passes_user_message_to_client(self):
        from app.llm.router import generate_response

        user_message = "こんにちは"
        with patch("app.llm.ollama_client.OllamaClient.generate", return_value="ok") as mock_gen:
            generate_response("system", user_message)

        args, kwargs = mock_gen.call_args
        all_args = list(args) + list(kwargs.values())
        assert user_message in all_args


class TestClaudeClientDummy:
    def test_generate_raises_not_implemented_error(self):
        from app.llm.claude_client import ClaudeClient

        client = ClaudeClient()

        with pytest.raises(NotImplementedError):
            client.generate("system prompt", "user message")

    def test_not_implemented_error_is_not_caught(self):
        from app.llm.claude_client import ClaudeClient

        client = ClaudeClient()

        with pytest.raises(NotImplementedError) as exc_info:
            client.generate("system", "user")

        assert exc_info.type is NotImplementedError

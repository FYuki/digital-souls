from unittest.mock import patch

import pytest


class TestGenerateResponse:
    def test_returns_ollama_client_output(self):
        from app.llm.router import generate_response

        prompt = object()
        with patch(
            "app.llm.ollama_client.OllamaClient.generate",
            return_value="光織のLLM応答",
        ):
            result = generate_response(prompt)

        assert result == "光織のLLM応答"

    def test_passes_built_prompt_to_client_unchanged(self):
        from app.llm.router import generate_response

        prompt = object()
        with patch(
            "app.llm.ollama_client.OllamaClient.generate",
            return_value="ok",
        ) as generate:
            generate_response(prompt)

        generate.assert_called_once_with(prompt)

class TestClaudeClientDummy:
    def test_generate_raises_not_implemented_error(self):
        from app.llm.router import _create_llm_client

        client = _create_llm_client("claude")

        with pytest.raises(NotImplementedError):
            client.generate(object())

    def test_router_does_not_expose_infrastructure_clients(self):
        from app.llm import router

        assert not hasattr(router, "__all__")
        assert not hasattr(router, "OllamaClient")
        assert not hasattr(router, "ClaudeClient")

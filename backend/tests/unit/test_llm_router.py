from unittest.mock import patch

import pytest


def _built_prompt():
    from tests.prompt_test_support import prompt_build_input, prompt_builder

    return prompt_builder().build(prompt_build_input())


def _settings():
    from app.model_settings import resolve_model_settings

    return resolve_model_settings({})


class TestGenerateResponse:
    def test_should_pass_built_prompt_to_selected_client(self):
        from app.llm.router import generate_response

        built_prompt = _built_prompt()
        with patch(
            "app.llm.ollama_client.OllamaClient.generate",
            return_value="光織のLLM応答",
        ) as generate:
            result = generate_response(
                built_prompt, max_output_tokens=512, settings=_settings()
            )

        assert result == "光織のLLM応答"
        generate.assert_called_once_with(built_prompt, max_output_tokens=512)


class TestClaudeClientDummy:
    def test_generate_raises_not_implemented_error(self):
        from app.llm.router import _create_llm_client

        client = _create_llm_client("claude", _settings())

        with pytest.raises(NotImplementedError):
            client.generate(_built_prompt(), max_output_tokens=512)

    def test_router_does_not_expose_infrastructure_clients(self):
        from app.llm import router

        assert not hasattr(router, "__all__")
        assert not hasattr(router, "create_llm_client")
        assert not hasattr(router, "OllamaClient")
        assert not hasattr(router, "ClaudeClient")

    def test_should_reject_unsupported_provider(self):
        from app.llm.router import _create_llm_client

        with pytest.raises(ValueError, match="Unsupported LLM provider: invalid"):
            _create_llm_client("invalid", _settings())

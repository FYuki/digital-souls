import importlib
import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.prompting import PromptMessage, PromptRole


PROMPT_ENV_KEYS = (
    "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS",
    "CONVERSATION_HISTORY_TOKEN_LIMIT",
    "USER_INPUT_TOKEN_LIMIT",
    "ASSISTANT_MAX_GENERATION_TOKENS",
    "LLM_CONTEXT_TOKEN_LIMIT",
)
_BACKEND_DIR = Path(__file__).parent.parent.parent


def _model_settings_module():
    return importlib.import_module("app.model_settings")


class TestModelSettings:
    def test_chat_runtime_should_require_resolved_prompt_config(self) -> None:
        from app._chat_runtime import ChatRuntimeConfig

        parameter = inspect.signature(ChatRuntimeConfig).parameters["prompt_config"]

        assert parameter.default is inspect.Parameter.empty

    def test_env_example_should_declare_prompt_runtime_defaults(self) -> None:
        lines = (_BACKEND_DIR / ".env.example").read_text().splitlines()

        assert {
            "OLLAMA_CHAT_MODEL=gemma4:e4b",
            "WHISPER_MODEL=medium",
            "OLLAMA_CONTEXT_TOKENS=8192",
            "OLLAMA_RESPONSE_RESERVE_TOKENS=1024",
            "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS=10",
            "CONVERSATION_HISTORY_TOKEN_LIMIT=4096",
            "USER_INPUT_TOKEN_LIMIT=8192",
            "ASSISTANT_MAX_GENERATION_TOKENS=1024",
            "LLM_CONTEXT_TOKEN_LIMIT=32768",
        }.issubset(lines)

    def test_should_resolve_documented_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in PROMPT_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)

        config = _model_settings_module().resolve_model_settings({})

        assert config.max_completed_turns == 10
        assert config.history_token_limit == 4096
        assert config.user_input_token_limit == 8192
        assert config.assistant_max_generation_tokens == 1024
        assert config.model_context_token_limit == 32768

    def test_should_propagate_environment_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        values = {
            "OLLAMA_CONTEXT_TOKENS": "7000",
            "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": "3",
            "CONVERSATION_HISTORY_TOKEN_LIMIT": "1200",
            "USER_INPUT_TOKEN_LIMIT": "600",
            "ASSISTANT_MAX_GENERATION_TOKENS": "700",
            "LLM_CONTEXT_TOKEN_LIMIT": "8000",
        }
        for key, value in values.items():
            monkeypatch.setenv(key, value)

        config = _model_settings_module().resolve_model_settings(values)

        assert (
            config.max_completed_turns,
            config.history_token_limit,
            config.user_input_token_limit,
            config.assistant_max_generation_tokens,
            config.model_context_token_limit,
        ) == (3, 1200, 600, 700, 8000)

    @pytest.mark.parametrize("key", PROMPT_ENV_KEYS)
    @pytest.mark.parametrize("value", ["0", "-1", "1.5", " 1", "invalid"])
    def test_should_reject_non_positive_or_non_canonical_integer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        key: str,
        value: str,
    ) -> None:
        for environment_key in PROMPT_ENV_KEYS:
            monkeypatch.delenv(environment_key, raising=False)
        monkeypatch.setenv(key, value)

        with pytest.raises(ValueError, match=key):
            _model_settings_module().resolve_model_settings({key: value})

    def test_should_reject_generation_reservation_equal_to_context_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ASSISTANT_MAX_GENERATION_TOKENS", "1024")
        monkeypatch.setenv("OLLAMA_CONTEXT_TOKENS", "1024")
        monkeypatch.setenv("LLM_CONTEXT_TOKEN_LIMIT", "1024")

        with pytest.raises(ValueError, match="ASSISTANT_MAX_GENERATION_TOKENS"):
            _model_settings_module().resolve_model_settings(
                {
                    "ASSISTANT_MAX_GENERATION_TOKENS": "1024",
                    "OLLAMA_CONTEXT_TOKENS": "1024",
                    "LLM_CONTEXT_TOKEN_LIMIT": "1024",
                }
            )


def _ollama_response(*, content: str = "reply", prompt_eval_count: object = 7):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "message": {"role": "assistant", "content": content},
        "prompt_eval_count": prompt_eval_count,
    }
    return response


class TestOllamaInputTokens:
    @pytest.mark.parametrize(
        ("module_name", "owner_name", "callable_name"),
        [
            ("app.llm.base", "LLMClient", "generate"),
            ("app.llm.ollama_client", "OllamaClient", "generate"),
            ("app.llm.router", "_ClaudeClient", "generate"),
            ("app.llm.router", None, "generate_response"),
        ],
    )
    def test_generation_should_require_max_output_tokens(
        self,
        module_name: str,
        owner_name: str | None,
        callable_name: str,
    ) -> None:
        module = importlib.import_module(module_name)
        owner = module if owner_name is None else getattr(module, owner_name)
        callable_object = getattr(owner, callable_name)

        parameter = inspect.signature(callable_object).parameters[
            "max_output_tokens"
        ]

        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    def test_should_count_tokens_through_chat_using_exact_messages(self) -> None:
        from app.llm.ollama_client import OllamaClient

        messages = (
            PromptMessage(PromptRole.SYSTEM, "system"),
            PromptMessage(PromptRole.USER, "user"),
        )
        with patch(
            "app.llm.ollama_client.httpx.post",
            return_value=_ollama_response(prompt_eval_count=13),
        ) as post:
            result = OllamaClient(
                model_name="gemma4:e4b",
                context_tokens=8192,
            ).count_input_tokens(messages)

        assert result == 13
        assert post.call_args.kwargs["json"]["messages"] == [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ]
        assert post.call_args.kwargs["json"]["options"]["num_predict"] == 1

    @pytest.mark.parametrize("value", [None, True, 0, -1, "7"])
    def test_should_reject_malformed_prompt_eval_count(self, value: object) -> None:
        from app.llm.ollama_client import OllamaClient

        with patch(
            "app.llm.ollama_client.httpx.post",
            return_value=_ollama_response(prompt_eval_count=value),
        ):
            with pytest.raises(ValueError, match="prompt_eval_count"):
                OllamaClient(
                    model_name="gemma4:e4b",
                    context_tokens=8192,
                ).count_input_tokens(
                    (PromptMessage(PromptRole.USER, "user"),)
                )

    def test_should_send_configured_generation_limit_to_ollama(self) -> None:
        from app.llm.ollama_client import OllamaClient
        from tests.prompt_test_support import prompt_build_input, prompt_builder

        prompt = prompt_builder().build(prompt_build_input())
        with patch(
            "app.llm.ollama_client.httpx.post",
            return_value=_ollama_response(),
        ) as post:
            OllamaClient(
                model_name="gemma4:e4b",
                context_tokens=8192,
            ).generate(prompt, max_output_tokens=777)

        assert post.call_args.kwargs["json"]["options"]["num_predict"] == 777

    def test_generation_and_counting_should_send_same_messages(self) -> None:
        from app.llm import ollama_client
        from tests.prompt_test_support import prompt_build_input, prompt_builder

        prompt = prompt_builder().build(prompt_build_input())
        with (
            patch(
                "app.llm.ollama_client.httpx.post",
                return_value=_ollama_response(),
            ) as post,
        ):
            client = ollama_client.OllamaClient(
                model_name="gemma4:e4b",
                context_tokens=8192,
            )
            client.count_input_tokens(prompt.messages)
            client.generate(prompt, max_output_tokens=777)

        counting_payload = post.call_args_list[0].kwargs["json"]
        generation_payload = post.call_args_list[1].kwargs["json"]

        assert counting_payload["messages"] == generation_payload["messages"]

    def test_router_should_delegate_count_and_generation_to_same_provider(self) -> None:
        from app.llm import router
        from tests.prompt_test_support import prompt_build_input, prompt_builder

        prompt = prompt_builder().build(prompt_build_input())
        client = MagicMock()
        client.count_input_tokens.return_value = 9
        client.generate.return_value = "reply"
        from app.model_settings import resolve_model_settings

        settings = resolve_model_settings({})
        with patch.object(router, "_create_llm_client", return_value=client):
            counted = router.count_input_tokens(prompt.messages, settings=settings)
            generated = router.generate_response(
                prompt, max_output_tokens=321, settings=settings
            )

        assert counted == 9
        assert generated == "reply"
        client.count_input_tokens.assert_called_once_with(prompt.messages)
        client.generate.assert_called_once_with(prompt, max_output_tokens=321)

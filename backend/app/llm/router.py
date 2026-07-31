from app.llm.base import LLMClient
from app.prompting import BuiltPrompt, PromptMessage

DEFAULT_PROVIDER = "ollama"


class _ClaudeClient(LLMClient):
    def generate(
        self,
        prompt: BuiltPrompt,
        *,
        max_output_tokens: int,
    ) -> str:
        raise NotImplementedError("ClaudeClient is not yet implemented")

    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        raise NotImplementedError("ClaudeClient is not yet implemented")


def _create_llm_client(provider: str) -> LLMClient:
    if provider == "ollama":
        from app.llm.ollama_client import OllamaClient as _OllamaClient

        return _OllamaClient()
    if provider == "claude":
        return _ClaudeClient()
    raise ValueError(f"Unsupported LLM provider: {provider}")


def generate_response(
    prompt: BuiltPrompt,
    *,
    max_output_tokens: int,
) -> str:
    client = _create_llm_client(DEFAULT_PROVIDER)
    return client.generate(prompt, max_output_tokens=max_output_tokens)


def count_input_tokens(messages: tuple[PromptMessage, ...]) -> int:
    client = _create_llm_client(DEFAULT_PROVIDER)
    return client.count_input_tokens(messages)

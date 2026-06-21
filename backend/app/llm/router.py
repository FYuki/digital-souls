from app.llm.base import LLMClient

DEFAULT_PROVIDER = "ollama"


class _ClaudeClient(LLMClient):
    def generate(self, system_prompt: str, user_message: str) -> str:
        raise NotImplementedError("ClaudeClient is not yet implemented")


def _create_llm_client(provider: str) -> LLMClient:
    if provider == "ollama":
        from app.llm.ollama_client import OllamaClient as _OllamaClient

        return _OllamaClient()
    if provider == "claude":
        return _ClaudeClient()
    raise ValueError(f"Unsupported LLM provider: {provider}")


def generate_response(system_prompt: str, user_message: str) -> str:
    client = _create_llm_client(DEFAULT_PROVIDER)
    return client.generate(system_prompt, user_message)

from app.llm.base import LLMClient


class ClaudeClient(LLMClient):
    def generate(self, system_prompt: str, user_message: str) -> str:
        raise NotImplementedError("ClaudeClient is not yet implemented")

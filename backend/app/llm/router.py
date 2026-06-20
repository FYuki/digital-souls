from app.llm.ollama_client import OllamaClient


def generate_response(system_prompt: str, user_message: str) -> str:
    client = OllamaClient()
    return client.generate(system_prompt, user_message)

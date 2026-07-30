from collections.abc import Mapping
from typing import cast

import httpx

from app.llm.base import LLMClient
from app.llm.ollama_config import ollama_endpoint, ollama_timeout
from app.model_settings import OLLAMA_MODEL_NAME
from app.prompting.types import BuiltPrompt


def _as_object_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Ollama response field '{field_name}' must be an object")
    return cast(Mapping[str, object], value)


def _extract_message_content(response_body: object) -> str:
    body = _as_object_mapping(response_body, "root")
    message = _as_object_mapping(body.get("message"), "message")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("Ollama response field 'message.content' must be a string")
    return content


class OllamaClient(LLMClient):
    def generate(self, prompt: BuiltPrompt) -> str:
        payload = {
            "model": OLLAMA_MODEL_NAME,
            "stream": False,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in prompt.messages
            ],
        }
        response = httpx.post(
            ollama_endpoint("/api/chat"),
            json=payload,
            timeout=ollama_timeout(),
        )
        response.raise_for_status()
        return _extract_message_content(response.json())

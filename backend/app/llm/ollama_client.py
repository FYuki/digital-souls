import os
from collections.abc import Mapping
from typing import cast

import httpx

from app.llm.base import LLMClient

_MODEL = "gemma4:e4b"
_OLLAMA_BASE_URL_ENV = "OLLAMA_BASE_URL"
_DEFAULT_BASE_URL = "http://localhost:11434"
_OLLAMA_TIMEOUT_SECONDS = 30.0


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
    def __init__(self) -> None:
        self._base_url = os.environ.get(_OLLAMA_BASE_URL_ENV, _DEFAULT_BASE_URL)

    def generate(self, system_prompt: str, user_message: str) -> str:
        url = f"{self._base_url}/api/chat"
        payload = {
            "model": _MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        response = httpx.post(
            url,
            json=payload,
            timeout=httpx.Timeout(_OLLAMA_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        return _extract_message_content(response.json())

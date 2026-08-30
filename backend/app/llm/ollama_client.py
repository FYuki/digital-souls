import json
from collections.abc import AsyncIterator, Mapping
from typing import cast

import httpx

from app.llm.base import LLMClient
from app.llm.ollama_config import ollama_endpoint, ollama_timeout
from app.prompting import BuiltPrompt, PromptMessage


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


def _serialize_messages(
    messages: tuple[PromptMessage, ...],
) -> list[dict[str, str]]:
    return [
        {"role": message.role.value, "content": message.content}
        for message in messages
    ]


class OllamaClient(LLMClient):
    def __init__(self, *, model_name: str, context_tokens: int) -> None:
        self._model_name = model_name
        self._context_tokens = context_tokens

    def generate(
        self,
        prompt: BuiltPrompt,
        *,
        max_output_tokens: int,
    ) -> str:
        payload = {
            "model": self._model_name,
            "stream": False,
            "messages": _serialize_messages(prompt.messages),
        }
        payload["options"] = {
            "num_ctx": self._context_tokens,
            "num_predict": max_output_tokens,
        }
        response = httpx.post(
            ollama_endpoint("/api/chat"),
            json=payload,
            timeout=ollama_timeout(),
        )
        response.raise_for_status()
        return _extract_message_content(response.json())

    def count_input_tokens(self, messages: tuple[PromptMessage, ...]) -> int:
        response = httpx.post(
            ollama_endpoint("/api/chat"),
            json={
                "model": self._model_name,
                "stream": False,
                "messages": _serialize_messages(messages),
                "options": {
                    "num_ctx": self._context_tokens,
                    "num_predict": 1,
                },
            },
            timeout=ollama_timeout(),
        )
        response.raise_for_status()
        body = _as_object_mapping(response.json(), "root")
        count = body.get("prompt_eval_count")
        if type(count) is not int or count < 1:
            raise ValueError(
                "Ollama response field 'prompt_eval_count' must be a positive integer"
            )
        return count

    async def stream_generate(
        self,
        prompt: BuiltPrompt,
        *,
        max_output_tokens: int,
    ) -> AsyncIterator[str]:
        payload = {
            "model": self._model_name,
            "stream": True,
            "messages": _serialize_messages(prompt.messages),
            "options": {
                "num_ctx": self._context_tokens,
                "num_predict": max_output_tokens,
            },
        }
        completed = False
        emitted = False
        async with httpx.AsyncClient(timeout=ollama_timeout()) as client:
            async with client.stream(
                "POST", ollama_endpoint("/api/chat"), json=payload
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    content, done, _done_reason = _parse_stream_chunk(line)
                    if content:
                        emitted = True
                        yield content
                    if done:
                        completed = True
                        break
        if not completed:
            raise ValueError("Ollama stream ended without a terminal chunk")
        if not emitted:
            raise ValueError("Ollama stream returned an empty response")


def _parse_stream_chunk(line: str) -> tuple[str, bool, str | None]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError("Ollama stream chunk is malformed JSON") from error
    body = _as_object_mapping(value, "stream chunk")
    done = body.get("done")
    if not isinstance(done, bool):
        raise ValueError("Ollama stream field 'done' must be a boolean")
    message = _as_object_mapping(body.get("message"), "message")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("Ollama stream field 'message.content' must be a string")
    done_reason = body.get("done_reason")
    if done_reason is not None and not isinstance(done_reason, str):
        raise ValueError("Ollama stream field 'done_reason' must be a string")
    return content, done, done_reason

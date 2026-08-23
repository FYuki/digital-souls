from __future__ import annotations

import httpx

from app.llm.ollama_config import resolve_ollama_base_url


class OllamaMemoryExtractorError(RuntimeError):
    """Ollama抽出要求が安全に完了しなかったことを表す。"""


class OllamaMemoryExtractorClient:
    def __init__(
        self,
        *,
        model_id: str,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not model_id.strip():
            raise ValueError("extractor model id must not be blank")
        self._model_id = model_id
        resolved_base_url = resolve_ollama_base_url() if base_url is None else base_url
        self._base_url = resolved_base_url.rstrip("/")
        self._http_client = http_client or httpx.Client(trust_env=False)
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._http_client.close()

    def chat(
        self,
        messages: tuple[dict[str, str], ...],
        *,
        json_schema: dict[str, object],
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> str:
        try:
            response = self._http_client.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": self._model_id,
                    "stream": False,
                    "format": json_schema,
                    "options": {"temperature": 0, "num_predict": max_output_tokens},
                    "messages": list(messages),
                },
                timeout=httpx.Timeout(timeout_seconds),
            )
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise TimeoutError("memory extractor request timed out") from error
        except httpx.HTTPError:
            raise OllamaMemoryExtractorError("memory extractor request failed") from None
        try:
            body: object = response.json()
        except ValueError:
            raise OllamaMemoryExtractorError("memory extractor response is invalid") from None
        if not isinstance(body, dict):
            raise OllamaMemoryExtractorError("memory extractor response is invalid")
        message = body.get("message")
        if not isinstance(message, dict):
            raise OllamaMemoryExtractorError("memory extractor response is invalid")
        content = message.get("content")
        if not isinstance(content, str):
            raise OllamaMemoryExtractorError("memory extractor response is invalid")
        return content

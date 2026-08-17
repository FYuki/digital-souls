from __future__ import annotations

import re

import httpx

from app.llm.ollama_config import resolve_ollama_base_url


_DIGEST_PATTERN = re.compile(r"sha256[:-]([0-9a-fA-F]{64})")


class OllamaClassifierError(RuntimeError):
    pass


class OllamaModelNotLoadedError(OllamaClassifierError):
    pass


class OllamaInvalidResponseError(OllamaClassifierError):
    pass


class OllamaClassifierClient:
    def __init__(self, *, model_id: str) -> None:
        if not model_id.strip():
            raise ValueError("classifier model id must not be blank")
        self._model_id = model_id
        self._base_url = resolve_ollama_base_url().rstrip("/")
        self._model_digest: str | None = None

    def chat(
        self,
        messages: tuple[dict[str, str], ...],
        *,
        timeout_seconds: float,
    ) -> str:
        try:
            response = httpx.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": self._model_id,
                    "stream": False,
                    "format": "json",
                    "messages": list(messages),
                },
                timeout=httpx.Timeout(timeout_seconds),
            )
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise TimeoutError("semantic classifier request timed out") from error
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                raise OllamaModelNotLoadedError() from None
            raise OllamaClassifierError("semantic classifier request failed") from None
        except httpx.HTTPError:
            raise OllamaClassifierError("semantic classifier request failed") from None
        body = self._response_object(response)
        message = body.get("message")
        if not isinstance(message, dict):
            raise OllamaInvalidResponseError("semantic classifier response is invalid")
        content = message.get("content")
        if not isinstance(content, str):
            raise OllamaInvalidResponseError("semantic classifier response is invalid")
        return content

    def resolve_model_digest(self) -> str:
        if self._model_digest is not None:
            return self._model_digest
        try:
            response = httpx.post(
                f"{self._base_url}/api/show",
                json={"model": self._model_id},
                timeout=httpx.Timeout(15.0),
            )
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise TimeoutError("semantic classifier model lookup timed out") from error
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                raise OllamaModelNotLoadedError() from None
            raise OllamaClassifierError("semantic classifier model lookup failed") from None
        except httpx.HTTPError:
            raise OllamaClassifierError("semantic classifier model lookup failed") from None
        body = self._response_object(response)
        modelfile = body.get("modelfile")
        if not isinstance(modelfile, str):
            raise OllamaInvalidResponseError("semantic classifier model metadata is invalid")
        match = _DIGEST_PATTERN.search(modelfile)
        if match is None:
            raise OllamaInvalidResponseError("semantic classifier model metadata is invalid")
        self._model_digest = f"sha256:{match.group(1).lower()}"
        return self._model_digest

    @staticmethod
    def _response_object(response: httpx.Response) -> dict[str, object]:
        try:
            body: object = response.json()
        except ValueError:
            raise OllamaInvalidResponseError(
                "semantic classifier response is invalid"
            ) from None
        if not isinstance(body, dict) or not all(
            isinstance(key, str) for key in body
        ):
            raise OllamaInvalidResponseError("semantic classifier response is invalid")
        return body

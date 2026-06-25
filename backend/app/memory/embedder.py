from collections.abc import Sequence

import httpx

from app.llm.ollama_config import (
    ollama_endpoint,
    ollama_timeout,
    resolve_ollama_embedding_model,
)

OLLAMA_EMBEDDINGS_PATH = "/api/embeddings"


def _extract_embedding(response_body: object) -> list[float]:
    if not isinstance(response_body, dict):
        raise ValueError("Ollama embedding response must be an object")
    embedding = response_body.get("embedding")
    if not isinstance(embedding, Sequence) or isinstance(embedding, str):
        raise ValueError("Ollama response field 'embedding' must be a number array")
    if not all(isinstance(value, int | float) for value in embedding):
        raise ValueError("Ollama response field 'embedding' must contain only numbers")
    return [float(value) for value in embedding]


def embed_text(text: str) -> list[float]:
    response = httpx.post(
        ollama_endpoint(OLLAMA_EMBEDDINGS_PATH),
        json={"model": resolve_ollama_embedding_model(), "prompt": text},
        timeout=ollama_timeout(),
    )
    response.raise_for_status()
    return _extract_embedding(response.json())

import os

import httpx

OLLAMA_BASE_URL_ENV = "OLLAMA_BASE_URL"
OLLAMA_EMBEDDING_MODEL_ENV = "OLLAMA_EMBEDDING_MODEL"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_OLLAMA_EMBEDDING_MODEL = "nomic-embed-text:latest"
OLLAMA_TIMEOUT_SECONDS = 30.0


def resolve_ollama_base_url() -> str:
    return os.environ.get(OLLAMA_BASE_URL_ENV, DEFAULT_OLLAMA_BASE_URL)


def resolve_ollama_embedding_model() -> str:
    return os.environ.get(
        OLLAMA_EMBEDDING_MODEL_ENV,
        DEFAULT_OLLAMA_EMBEDDING_MODEL,
    )


def ollama_endpoint(path: str) -> str:
    return f"{resolve_ollama_base_url()}{path}"


def ollama_timeout() -> httpx.Timeout:
    return httpx.Timeout(OLLAMA_TIMEOUT_SECONDS)

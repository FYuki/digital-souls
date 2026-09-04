import os

import httpx

OLLAMA_BASE_URL_ENV = "OLLAMA_BASE_URL"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_TIMEOUT_SECONDS = 30.0


def resolve_ollama_base_url() -> str:
    return os.environ.get(OLLAMA_BASE_URL_ENV, DEFAULT_OLLAMA_BASE_URL)


def ollama_endpoint(path: str) -> str:
    return f"{resolve_ollama_base_url()}{path}"


def ollama_timeout() -> httpx.Timeout:
    return httpx.Timeout(OLLAMA_TIMEOUT_SECONDS)

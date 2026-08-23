from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit


def require_local_ollama_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("consolidation Ollama base URL must be a local HTTP URL")
    if hostname.casefold() != "localhost" and not _is_loopback_address(hostname):
        raise ValueError("consolidation Ollama base URL must use a loopback host")
    return base_url.rstrip("/")


def _is_loopback_address(hostname: str) -> bool:
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False

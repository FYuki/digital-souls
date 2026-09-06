from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


SCREEN_ALLOWED_ORIGIN_ENV = "SCREEN_ALLOWED_ORIGIN"
CLIENT_SESSION_COOKIE = "digital_souls_screen_client"


@dataclass(frozen=True)
class ScreenHttpSecurity:
    allowed_origin: str


def resolve_screen_http_security(value: str) -> ScreenHttpSecurity:
    if not value or value.strip() != value:
        raise ValueError("SCREEN_ALLOWED_ORIGIN must not be empty or padded")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("SCREEN_ALLOWED_ORIGIN must be a canonical HTTP origin")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if value.rstrip("/") != origin:
        raise ValueError("SCREEN_ALLOWED_ORIGIN must be a canonical HTTP origin")
    return ScreenHttpSecurity(origin)

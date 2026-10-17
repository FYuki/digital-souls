"""Loopback authority/origin checks; these are not caller authentication."""

import re
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def local_authority(value: str, scheme: str) -> tuple[str, int] | None:
    """Normalize only unambiguous loopback authorities, including explicit ports."""
    if (
        scheme not in {"http", "https"}
        or re.fullmatch(r"(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]+)?", value, re.IGNORECASE)
        is None
    ):
        return None
    try:
        parsed = urlsplit("//" + value)
        if (
            parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or value.endswith(":")
        ):
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
        if parsed.port == 0:
            return None
        return parsed.hostname, port
    except ValueError:
        return None


class LocalHTTPBoundary:
    """Reject rebinding Host and cross-origin browser requests before body parsing.

    CLI clients without Origin remain supported. This cannot authenticate another
    local process, so the service remains single-user and bound to loopback.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        hosts = [
            value.decode("latin-1") for key, value in scope["headers"] if key.lower() == b"host"
        ]
        origins = [
            value.decode("latin-1") for key, value in scope["headers"] if key.lower() == b"origin"
        ]
        authority = local_authority(hosts[0], scope["scheme"]) if len(hosts) == 1 else None
        code, status = "invalid_host", 400
        accepted = authority is not None
        if accepted and origins:
            code, status = "origin_denied", 403
            accepted = False
            if len(origins) == 1 and not any(
                ord(char) <= 32 or ord(char) == 127 for char in origins[0]
            ):
                try:
                    origin = urlsplit(origins[0])
                    accepted = (
                        origin.scheme == scope["scheme"]
                        and not (origin.path or origin.query or origin.fragment)
                        and local_authority(origin.netloc, origin.scheme) == authority
                    )
                except ValueError:
                    pass
        if not accepted:
            response = JSONResponse(
                {
                    "error": {
                        "type": "core_error",
                        "code": code,
                        "message": "Local HTTP boundary rejected the request",
                    }
                },
                status_code=status,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

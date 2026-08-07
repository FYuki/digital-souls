from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from profile_types import ProfileError


@dataclass(frozen=True)
class ManagedHttpEndpoint:
    base_url: str
    host: str
    port: int


def resolve_managed_http_origin(base_url: object, path: str) -> ManagedHttpEndpoint:
    if not isinstance(base_url, str):
        raise ProfileError(f"{path} must be a string")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as error:
        raise ProfileError(f"{path} must be a valid HTTP loopback origin") from error
    if parsed.scheme != "http" or parsed.hostname is None:
        raise ProfileError(f"{path} must be an HTTP loopback origin")
    if parsed.username is not None or parsed.password is not None:
        raise ProfileError(f"{path} must not contain user information")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ProfileError(f"{path} must be an origin without path, query, or fragment")
    if port is None or not 1 <= port <= 65535:
        raise ProfileError(f"{path} must specify a port between 1 and 65535")
    if parsed.netloc.rsplit(":", 1)[-1] != str(port):
        raise ProfileError(f"{path} must specify a canonical port")
    if not _is_loopback(parsed.hostname):
        raise ProfileError(f"{path} must use a loopback host")
    return ManagedHttpEndpoint(base_url=base_url, host=parsed.hostname, port=port)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    if "%" in host:
        return False
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False

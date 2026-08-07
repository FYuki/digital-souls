from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "base_url",
    [
        "http://0.0.0.0:8000",
        "http://192.168.1.20:8000",
        "https://localhost:8000",
        "http://localhost",
        "http://localhost:0",
        "http://localhost:65536",
        "http://localhost:08000",
        "http://[1]:8000",
        "http://[::127.0.0.1]:8000",
        "http://[::ffff:127.0.0.1]:8000",
        "http://[0:0:0:0:0:0:0.0.0.2]:8000",
        "http://user@localhost:8000",
        "http://localhost:8000/api",
        "http://localhost:8000?mode=dev",
        "not-a-url",
    ],
)
def test_should_reject_unusable_managed_endpoint_when_resolving_origin(
    base_url: str,
) -> None:
    from managed_endpoint import resolve_managed_http_origin
    from profile_types import ProfileError

    with pytest.raises(ProfileError, match=r"backend\.baseUrl"):
        resolve_managed_http_origin(base_url, "backend.baseUrl")


@pytest.mark.parametrize(
    ("base_url", "host", "port"),
    [
        ("HTTP://localhost:18000", "localhost", 18000),
        ("http://localhost:18000", "localhost", 18000),
        ("http://127.0.0.1:18000", "127.0.0.1", 18000),
        ("http://127.42.0.7:1", "127.42.0.7", 1),
        ("http://127.255.255.255:65535", "127.255.255.255", 65535),
        ("http://[::1]:18000", "::1", 18000),
        ("http://[0:0:0:0:0:0:0:1]:18000", "0:0:0:0:0:0:0:1", 18000),
        ("http://[::01]:18000", "::01", 18000),
        ("http://[::0.0.0.1]:18000", "::0.0.0.1", 18000),
        ("http://[::0000:0.0.0.1]:18000", "::0000:0.0.0.1", 18000),
        ("http://[::0:0.0.0.1]:18000", "::0:0.0.0.1", 18000),
        ("http://[::00:0.0.0.1]:18000", "::00:0.0.0.1", 18000),
        ("http://[::000:0.0.0.1]:18000", "::000:0.0.0.1", 18000),
        (
            "http://[::0000:0000:0.0.0.1]:18000",
            "::0000:0000:0.0.0.1",
            18000,
        ),
        ("http://[0::0000:0.0.0.1]:18000", "0::0000:0.0.0.1", 18000),
        (
            "http://[0:0:0:0:0:0:0.0.0.1]:18000",
            "0:0:0:0:0:0:0.0.0.1",
            18000,
        ),
    ],
)
def test_should_resolve_loopback_managed_origin_to_one_host_and_port(
    base_url: str, host: str, port: int
) -> None:
    from managed_endpoint import resolve_managed_http_origin

    endpoint = resolve_managed_http_origin(base_url, "backend.baseUrl")

    assert endpoint.base_url == base_url
    assert endpoint.host == host
    assert endpoint.port == port

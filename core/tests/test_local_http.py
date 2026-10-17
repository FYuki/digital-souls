import pytest
from fastapi.testclient import TestClient

from digital_souls_core.api import create_app
from digital_souls_core.application import Inference

from .support import FakeProvider, character

pytestmark = pytest.mark.it1


@pytest.mark.parametrize(
    "host", ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "LOCALHOST:8000"]
)
def test_local_cli_and_same_origin_remain_usable(host: str) -> None:
    fake = FakeProvider()
    http = TestClient(create_app(Inference((character(),), fake)), base_url="http://127.0.0.1:8000")
    body = {"character_id": "miori", "messages": [{"role": "user", "content": "hi"}]}
    for headers in ({"host": host}, {"host": host, "origin": f"http://{host}"}):
        assert http.post("/v1/character/completions", json=body, headers=headers).status_code == 200
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "127.0.0.1.evil.example",
        "localhost@evil.example",
        "evil.example@localhost",
        "localhost/path",
        "localhost?x=1",
        "localhost#x",
        "localhost:0",
        "localhost:99999",
        "localhost:",
        "127.0.0.2",
        "localhost.",
        " localhost",
        "localhost\t",
        "[::1]evil.example",
        "[localhost]",
        "[::1]:+8000",
    ],
)
def test_untrusted_or_ambiguous_hosts_are_rejected_before_body_parsing(host: str) -> None:
    fake = FakeProvider()
    http = TestClient(create_app(Inference((character(),), fake)), base_url="http://127.0.0.1:8000")
    response = http.post(
        "/v1/character/completions", headers={"host": host}, content="SYNTHETIC_PRIVATE_NON_JSON"
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_host"
    assert "SYNTHETIC_PRIVATE" not in response.text and not fake.calls


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://localhost:3000",
        "http://127.0.0.1:8001",
        "https://127.0.0.1:8000",
        "null",
        "file://",
        "http://evil.example@127.0.0.1:8000",
        "http://127.0.0.1:8000/path",
        "http://127.0.0.1:8000#x",
        "http://127.0.0.1:8000\t",
    ],
)
def test_cross_origin_and_opaque_origins_cannot_use_enabled_profile(origin: str) -> None:
    fake = FakeProvider()
    http = TestClient(create_app(Inference((character(),), fake)), base_url="http://127.0.0.1:8000")
    response = http.post(
        "/v1/character/completions",
        headers={"origin": origin},
        json={"character_id": "miori", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 403 and response.json()["error"]["code"] == "origin_denied"
    assert not fake.calls


def test_duplicate_authority_headers_are_denied() -> None:
    http = TestClient(create_app(), base_url="http://127.0.0.1")
    assert (
        http.get(
            "/v1/models", headers=[("host", "localhost"), ("host", "evil.example")]
        ).status_code
        == 400
    )
    assert (
        http.get(
            "/v1/models",
            headers=[("origin", "http://127.0.0.1"), ("origin", "http://evil.example")],
        ).status_code
        == 403
    )

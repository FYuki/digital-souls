from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.screen_perception import router
from app.screen_perception.http_security import (
    CLIENT_SESSION_COOKIE,
    resolve_screen_http_security,
)
from app.screen_perception.service import RoutingPolicy, ScreenPerceptionService
from app.screen_perception.vision import VisionObservation


ORIGIN = "http://localhost:5173"
CONVERSATION_ID = "30000000-0000-4000-8000-000000000001"


class FakeVision:
    def observe(self, **_request: object) -> VisionObservation:
        raise AssertionError("境界拒否時にVisionを呼んではならない")


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.screen_http_security = resolve_screen_http_security(ORIGIN)
    app.state.screen_perception_service = ScreenPerceptionService(
        vision=FakeVision(),  # type: ignore[arg-type]
        routing_policy=lambda: RoutingPolicy("a" * 64, "local", "local"),
        validate_context=lambda _character, _conversation: None,
    )
    return app


def _session_start(client_session_id: str) -> dict[str, object]:
    return {
        "protocol_version": "1.0",
        "type": "screen_session_start_requested",
        "event_id": "10000000-0000-4000-8000-000000000002",
        "client_session_id": client_session_id,
        "generation": 1,
        "character_id": "miori",
        "conversation_id": CONVERSATION_ID,
        "requested_surface": "monitor",
        "actual_surface": "monitor",
        "routing_revision": "a" * 64,
        "cloud_consent": {
            "cloud_vision": False,
            "cloud_derived_chat": False,
        },
    }


def test_routing_issues_nonpersistent_owner_cookie_and_exact_origin_is_required() -> None:
    with TestClient(_app(), base_url=ORIGIN) as client:
        routing = client.get("/perception/screen/routing")
        client_session_id = routing.json()["client_session_id"]

        assert routing.status_code == 200
        set_cookie = routing.headers["set-cookie"]
        assert f"{CLIENT_SESSION_COOKIE}={client_session_id}" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=strict" in set_cookie
        assert "Max-Age" not in set_cookie
        assert "Expires" not in set_cookie

        wrong_origin = client.post(
            "/perception/screen/sessions",
            headers={"Origin": "http://localhost:5174"},
            json=_session_start(client_session_id),
        )
        accepted = client.post(
            "/perception/screen/sessions",
            headers={"Origin": ORIGIN},
            json=_session_start(client_session_id),
        )

        assert wrong_origin.status_code == 403
        assert accepted.status_code == 201
        assert accepted.json()["client_session_id"] == client_session_id


def test_claimed_client_owner_cannot_be_replaced_by_request_body() -> None:
    with TestClient(_app(), base_url=ORIGIN) as client:
        client.get("/perception/screen/routing")
        forged = _session_start("20000000-0000-4000-8000-000000000099")

        response = client.post(
            "/perception/screen/sessions",
            headers={"Origin": ORIGIN},
            json=forged,
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "context_mismatch"


def test_upload_rejects_declared_oversize_before_parsing_or_provider_send() -> None:
    with TestClient(_app(), base_url=ORIGIN) as client:
        client.get("/perception/screen/routing")

        response = client.put(
            "/perception/screen/requests/50000000-0000-4000-8000-000000000001/image",
            headers={
                "Origin": ORIGIN,
                "Content-Type": "image/png",
                "Content-Length": "5242881",
            },
            content=b"x",
        )

        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "image_too_large"

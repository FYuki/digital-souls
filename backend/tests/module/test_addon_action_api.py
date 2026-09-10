"""回答APIはキューを更新するだけで、外部操作を再開しない。"""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.addon_action.models import (
    ApprovalChoice,
    ApprovalKey,
    ExecutionScene,
    OperationGroup,
)
from app.addon_action.store import ActionStore
from app.external_mcp import Connection, Registry
from app.routers.addon_actions import router
from tests.external_mcp_test_support import manifest


def test_answer_checks_request_identity_is_idempotent_and_keeps_expired_queue(tmp_path):
    store = ActionStore(tmp_path / "actions.sqlite3")
    registry = Registry()
    connection = Connection.from_manifest(manifest())
    registry.register(connection)
    key = ApprovalKey(
        connection.id,
        connection.identity,
        OperationGroup.HIGH_IMPACT,
        ExecutionScene.AUTONOMOUS,
    )
    pending = store.enqueue(
        key,
        character_id="miori",
        session_id="activity",
        loop_id="loop",
        fingerprint="test",
        preview={"operation": "test"},
        wait_seconds=60,
        now=0,
    )
    store.end_wait(pending.id)
    app = FastAPI()
    app.include_router(router)
    app.state.action_policy = SimpleNamespace(store=store)
    app.state.addon_manager = SimpleNamespace(gate=SimpleNamespace(registry=registry))
    with TestClient(app) as client:
        url = f"/addon-actions/requests/{pending.id}/answer"
        body = {"choice": "once", "character": "miori", "session_id": "activity"}
        assert client.post(url, json={**body, "session_id": "other"}).status_code == 409
        assert store.state(key).remaining == 0
        for _ in range(2):
            result = client.post(url, json=body)
            assert result.status_code == 200
            assert result.headers["cache-control"] == "no-store"
            assert result.json()["request"]["waiting"] is False
        assert store.state(key).remaining == 1
        assert client.post(url, json={**body, "choice": "always"}).status_code == 409
        listing = client.get(
            "/addon-actions/requests",
            params={"character": "miori", "session_id": "activity"},
        )
        assert len(listing.json()["requests"]) == 1
        assert "fingerprint" not in listing.text and "identity" not in listing.text
        assert (
            client.get(
                "/addon-actions/requests", params={"session_id": "other"}
            ).json()["requests"]
            == []
        )
        invalid = client.post(url, json={**body, "choice": "secret-value"})
        assert invalid.status_code == 422 and "secret-value" not in invalid.text
        registry.remove(connection.id)
        assert client.post(url, json=body).status_code == 409
    assert store.request(pending.id).choice == ApprovalChoice.ONCE


def test_runtime_exposes_queue_without_tool_routing(client):
    assert client.app.state.tool_service is None
    result = client.get("/addon-actions/requests")
    assert result.status_code == 200
    assert result.json() == {"requests": []}

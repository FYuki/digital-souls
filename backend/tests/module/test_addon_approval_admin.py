"""#305 管理UIの承認正本・取り消し・遅い回答・接続世代をHTTPで検証する。"""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.addon_action.models import ApprovalChoice, ApprovalKey, ExecutionScene, OperationGroup, Permission
from app.external_mcp import Connection, Registry
from app.routers.addon_actions import router
from tests.addon_action_test_support import policy
from tests.external_mcp_test_support import manifest


@pytest.fixture
def admin(tmp_path):
    clock = [100.0]
    p = policy(tmp_path, clock=lambda: clock[0])
    registry = Registry()
    connection = Connection.from_manifest(manifest())
    registry.register(connection)
    key = ApprovalKey(connection.id, connection.identity, OperationGroup.HIGH_IMPACT, ExecutionScene.AUTONOMOUS)
    app = FastAPI()
    app.include_router(router)
    app.state.action_policy = p
    app.state.addon_manager = SimpleNamespace(gate=SimpleNamespace(registry=registry))
    with TestClient(app) as client:
        yield client, p, registry, key, clock


def enqueue(p, key, **changes):
    args = dict(character_id="miori", session_id=str(uuid4()), loop_id=str(uuid4()),
                fingerprint=str(uuid4()), preview={"connection": "検証接続", "operation": "write", "target": "検証対象", "arguments": {}}, wait_seconds=60)
    args.update(changes)
    return p.store.enqueue(key, **args)


def setting_body(client, key, permission):
    rows = client.get("/addon-actions/admin/permissions").json()["permissions"]
    row = next(r for r in rows if r["operation_group"] == key.group and r["scene"] == key.scene)
    return {name: row[name] for name in ("connection_id", "connection_token", "operation_group", "scene")} | {"permission": permission}


def test_settings_reset_revokes_future_and_reserved_once_and_keeps_other_scopes(admin):
    client, p, _, key, _ = admin
    other = replace(key, scene=ExecutionScene.CONVERSATION)
    p.store.choose(other, ApprovalChoice.ALWAYS)
    past = enqueue(p, key)
    p.store.end_wait(past.id)
    live = enqueue(p, key)
    for item in (past, live):
        response = client.post(f"/addon-actions/admin/requests/{item.id}/answer", json={"choice": "once"})
        assert response.status_code == 200
    assert p.store.settings(key) == {"permission": Permission.UNAPPROVED, "remaining": 1, "reserved": 1}
    body = setting_body(client, key, "unapproved")
    for _ in range(2):
        reset = client.put("/addon-actions/admin/permissions", json=body)
        assert reset.status_code == 200 and reset.headers["cache-control"] == "no-store"
    assert not p.store.consume_request(key, live.id, 100)
    assert not p.store.consume(key)
    assert not p.store.request(live.id).waiting
    assert p.store.state(other).permission == Permission.ALWAYS
    for item in (past, live):
        # 古い回答のネットワーク再送から単回許可を再発行しない。
        assert client.post(f"/addon-actions/admin/requests/{item.id}/answer", json={"choice": "once"}).status_code == 200
    assert p.store.settings(key) == {"permission": Permission.UNAPPROVED, "remaining": 0, "reserved": 0}


def test_denial_can_be_reset_without_granting_permission_and_normal_is_not_affected(admin):
    client, p, registry, key, _ = admin
    registry.set_enabled(key.connection_id, False)
    for permission in ("denied", "unapproved", "always"):
        response = client.put("/addon-actions/admin/permissions", json=setting_body(client, key, permission))
        assert response.status_code == 200
        assert p.store.state(key).permission == permission
        assert p.store.state(replace(key, group=OperationGroup.NORMAL)).permission == Permission.ALWAYS
        assert not registry.entry(key.connection_id).desired_enabled
    conversation = replace(key, scene=ExecutionScene.CONVERSATION)
    response = client.put("/addon-actions/admin/permissions", json=setting_body(client, conversation, "denied"))
    assert response.status_code == 409
    assert p.store.state(conversation).permission == Permission.UNAPPROVED


def test_old_connection_cannot_receive_settings_or_answer(admin):
    client, p, registry, key, _ = admin
    item = enqueue(p, key)
    body = setting_body(client, key, "always")
    registry.remove(key.connection_id)
    changed = manifest(connection_id=key.connection_id)
    changed["connection"]["stdio"]["args"] = ["new-server"]
    registry.register(Connection.from_manifest(changed))
    assert client.put("/addon-actions/admin/permissions", json=body).status_code == 409
    assert client.post(f"/addon-actions/admin/requests/{item.id}/answer", json={"choice": "once"}).status_code == 409
    assert not client.get("/addon-actions/admin/requests").json()["requests"][0]["connection_available"]
    assert p.store.state(key).permission == Permission.UNAPPROVED


def test_queue_pages_all_unanswered_even_after_200_newer_answers_and_keeps_expired(admin):
    client, p, _, key, clock = admin
    ids = {enqueue(p, key).id for _ in range(4)}
    for _ in range(201):
        item = enqueue(p, key)
        p.store.answer(item.id, ApprovalChoice.REJECT)
    clock[0] = 161
    seen, before = set(), None
    while True:
        params = {"limit": 2}
        if before:
            params["before"] = before
        response = client.get("/addon-actions/admin/requests", params=params)
        assert response.headers["cache-control"] == "no-store"
        page = response.json()
        assert all(not r["waiting"] for r in page["requests"])
        seen.update(r["id"] for r in page["requests"])
        before = page["next_cursor"]
        if before is None:
            break
    assert seen == ids
    rid = next(iter(ids))
    assert client.post(f"/addon-actions/admin/requests/{rid}/answer", json={"choice": "once"}).status_code == 200
    assert p.store.consume(key) and not p.store.consume(key)
    assert len(client.get("/addon-actions/admin/requests", params={"unanswered": False, "limit": 100}).json()["requests"]) == 100


def test_admin_projection_redacts_registered_secret_and_validation_never_echoes_input(admin):
    client, p, _, key, _ = admin
    p.sanitizer = replace(p.sanitizer, private_values=("secret-admin-token",))
    enqueue(p, key, preview={"connection": "secret-admin-token", "operation": "write", "target": "target", "arguments": {"value": "secret-admin-token"}})
    result = client.get("/addon-actions/admin/requests")
    assert "secret-admin-token" not in result.text and "fingerprint" not in result.text
    body = setting_body(client, key, "secret-admin-token")
    result = client.put("/addon-actions/admin/permissions", json=body)
    assert result.status_code == 422 and "secret-admin-token" not in result.text


@pytest.mark.parametrize("invalid_voice", [False, True])
def test_admin_continue_rejects_malformed_saved_subject_without_echoing(admin, invalid_voice):
    client, p, _, key, _ = admin
    key = replace(key, scene=ExecutionScene.CONVERSATION)
    invalid = "private-malformed-session"
    item = enqueue(p, key, session_id=str(uuid4()) if invalid_voice else invalid)
    p.store.answer(item.id, ApprovalChoice.ONCE)
    if invalid_voice:
        client.app.state.livekit_runtime_manager = SimpleNamespace(confirmation_session=lambda *_: invalid)
    response = client.post(f"/addon-actions/admin/requests/{item.id}/continue")
    assert response.status_code == 409
    assert invalid not in response.text
    assert p.store.request(item.id).once_reserved


def test_saved_approval_is_shared_but_live_reservation_remains_request_scoped(admin):
    client, p, _, key, _ = admin
    first = enqueue(p, key, character_id="miori")
    second = enqueue(p, key, character_id="other-character")
    assert client.post(f"/addon-actions/admin/requests/{first.id}/answer", json={"choice": "once"}).status_code == 200
    assert not p.store.consume_request(key, second.id, 100)
    assert p.store.consume_request(key, first.id, 100)
    assert client.put("/addon-actions/admin/permissions", json=setting_body(client, key, "always")).status_code == 200
    assert p.store.consume_request(key, second.id, 100)

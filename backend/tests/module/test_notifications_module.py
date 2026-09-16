"""独立MCP processを使う通知適合試験。実サービス受入とは分ける。"""
import json
from dataclasses import replace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.addon_events.contracts import Operation
from app.external_mcp.models import MCPFailure, digest, encode
from app.notifications.contracts import Limits, Registration
from app.notifications.runtime import NotificationRuntime
from app.notifications.store import NotificationStore
from app.routers.notifications import router
from tests.addon_events_test_support import Clock, connected, profile, runtime


@pytest.fixture
def anyio_backend():
    return "asyncio"


def registration(gate, **kwargs):
    entry = gate.registry.entry("external-test")
    snapshot = entry.active or entry.staged
    item = next(d for d in snapshot.document["tools"] if d["name"] == "detail")
    values = dict(id="rule", source_id="fixture", character_id="miori", event_type="task.completed",
                  detail=Operation("tool", "detail", digest(item["native_definition"])),
                  decision="notify", reference_arguments_json=encode({"task_ref": "task_ref"}))
    return Registration(**(values | kwargs))


def notifications(gate, events, regs, path, clock, limits=Limits()):
    store = NotificationStore(path / "notifications.sqlite3", limits, clock=clock)
    return NotificationRuntime(gate, events, regs, path / "notifications.sqlite3", events.reader.sanitizer,
                               character_exists=lambda c: c == "miori", store=store)


@pytest.mark.anyio
async def test_individual_replay_expiry_and_independent_reference(tmp_path):
    clock = Clock()
    async with connected(tmp_path / "provider") as (control, gate, _):
        source = profile(gate)
        events = runtime(gate, source, tmp_path, clock)
        reg = registration(gate)
        service = notifications(gate, events, (reg,), tmp_path, clock)
        try:
            await service.consume_once(reg.id)
            control.update(position=12, mode="duplicate")
            clock.advance()
            await events.poll_once(source.id)
            await service.consume_once(reg.id)
            result = await service.listing("local")
            assert result["unread_count"] == 2
            assert len(result["items"]) == 2
            row = result["items"][0]
            stored = service.store.get(row["id"], "local")
            ref = service.reader.reference(reg.id, stored["event_key"], json.loads(stored["metadata"]))
            await service.consume_once(reg.id)
            assert (await service.listing("local"))["total"] == 2
            detail = await service.detail(row["id"], "local")
            assert detail["state"] == "available"
            assert "synthetic-secret" not in detail["text"] and "private.invalid" not in detail["text"]
            assert (await service.listing("local"))["unread_count"] == 2
            await service.set_state(row["id"], "local", "read", row["version"])
            clock.advance(30 * 86400)
            assert (await service.listing("local"))["total"] == 0
            assert (await service.reader.read(ref, "local"))["state"] == "available"
            control.update(detail_state="deleted")
            assert (await service.reader.read(ref, "local"))["state"] == "deleted"
            control.update(detail_state="expired")
            assert (await service.reader.read(ref, "local"))["state"] == "expired"
            assert (await service.reader.read(ref, "other"))["state"] == "permission_denied"
            assert (await service.reader.read(ref, "local", purpose="registered_consumer", caller_character_id="other"))["state"] == "permission_denied"
            dump = "\n".join(service.store.db.iterdump())
            assert "synthetic-secret" not in dump and "private.invalid" not in dump and "合成通知の詳細" not in dump
        finally:
            await service.close()
            await events.close()


@pytest.mark.anyio
async def test_off_received_events_are_not_backfilled_after_restart(tmp_path):
    clock = Clock()
    async with connected(tmp_path / "provider") as (control, gate, _):
        source = profile(gate)
        events = runtime(gate, source, tmp_path, clock)
        reg = registration(gate, decision="ignore")
        service = notifications(gate, events, (reg,), tmp_path, clock)
        try:
            await service.consume_once(reg.id)
            control.update(position=13)
            clock.advance()
            await events.poll_once(source.id)
            # 通知consumerが未処理でも、OFF中に共有sourceが受信した分は遡らない。
            await service.set_preference("local", source.id, reg.event_type, True)
            await service.close()
            service = notifications(gate, events, (reg,), tmp_path, clock)
            await service.consume_once(reg.id)
            assert (await service.listing("local"))["total"] == 0
            control.update(position=14)
            clock.advance()
            await events.poll_once(source.id)
            await service.consume_once(reg.id)
            assert (await service.listing("local"))["total"] == 1
            await service.set_preference("local", source.id, reg.event_type, False)
            control.update(position=15)
            clock.advance()
            await events.poll_once(source.id)
            await service.consume_once(reg.id)
            assert (await service.listing("local"))["total"] == 1
            assert events.store.state(source.id)["position"] == 15
        finally:
            await service.close()
            await events.close()


@pytest.mark.anyio
async def test_api_concurrent_read_and_new_event_isolation(tmp_path):
    clock = Clock()
    async with connected(tmp_path / "provider") as (control, gate, _):
        source = profile(gate)
        events = runtime(gate, source, tmp_path, clock)
        reg = registration(gate)
        service = notifications(gate, events, (reg,), tmp_path, clock, Limits(max_per_user=2))
        app = FastAPI()
        app.state.notifications = service
        app.include_router(router)
        try:
            await service.consume_once(reg.id)
            control.update(position=12)
            clock.advance()
            await events.poll_once(source.id)
            await service.consume_once(reg.id)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/notifications")
                assert response.headers["cache-control"] == "no-store"
                row = response.json()["items"][0]
                response = await client.patch("/notifications/" + row["id"], json={"state": "read", "version": 1})
                assert response.status_code == 200
                response = await client.patch("/notifications/" + row["id"], json={"state": "hidden", "version": 1})
                assert response.status_code == 409
                control.update(position=13)
                clock.advance()
                await events.poll_once(source.id)
                await service.consume_once(reg.id)
                response = (await client.get("/notifications?unread=true")).json()
                assert all(r["state"] == "unread" for r in response["items"])
                assert response["retention"]["history_incomplete"]
                assert response["retention"]["evicted_count"] == 1
                assert (await client.get("/notifications?character_id=other")).json()["total"] == 0
                assert (await client.patch("/notifications/" + row["id"], json={"state": "read", "version": 2, "user_id": "other"})).status_code == 422
                assert (await service.listing("other"))["total"] == 0
        finally:
            await service.close()
            await events.close()


@pytest.mark.anyio
async def test_fixed_result_revision_and_revocation(tmp_path):
    clock = Clock()
    async with connected(tmp_path / "provider", sharing={"mode": "user_bound", "user_id": "owner"}) as (control, gate, _):
        source = replace(profile(gate), user_id="owner")
        events = runtime(gate, source, tmp_path, clock)
        reg = registration(gate, owner_user_id="owner", auth_user_id="owner", viewers=("other",), kind="result",
                           match_json=encode({"task_ref": "task-fixed", "execution_ref": "run-1"}),
                           reference_arguments_json=encode({"task_ref": "task_ref", "execution_ref": "execution_ref", "revision": "revision"}))
        service = notifications(gate, events, (reg,), tmp_path, clock)
        try:
            await service.consume_once(reg.id)
            refs = {"task_ref": "task-fixed", "execution_ref": "run-1", "revision": "1", "resource_ref": "safe-ref"}
            control.update(position=12, event_metadata=refs, detail_references=refs)
            clock.advance()
            await events.poll_once(source.id)
            await service.consume_once(reg.id)
            result = await service.listing("owner")
            assert result["total"] == 1  # 同じ結果revisionを別event IDで配信しても1件。
            row = result["items"][0]
            assert (await service.detail(row["id"], "owner"))["state"] == "available"
            assert (await service.listing("other"))["total"] == 0
            assert service.store.db.execute("SELECT count(*) FROM notifications WHERE user_id=?", ("other",)).fetchone()[0] == 0
            control.update(detail_references=refs | {"execution_ref": "run-2"})
            assert (await service.detail(row["id"], "owner"))["state"] == "revision_mismatch"
            service.reader.character_exists = lambda _: False
            assert (await service.listing("owner"))["total"] == 0
            with pytest.raises(MCPFailure):
                await service.detail(row["id"], "owner")
        finally:
            await service.close()
            await events.close()


@pytest.mark.anyio
async def test_notification_commit_survives_failed_ack_and_backend_restart(tmp_path, monkeypatch):
    clock = Clock()
    async with connected(tmp_path / "provider") as (control, gate, _):
        source = profile(gate)
        events = runtime(gate, source, tmp_path, clock)
        reg = registration(gate)
        service = notifications(gate, events, (reg,), tmp_path, clock)
        try:
            await service.consume_once(reg.id)
            control.update(position=11)
            clock.advance()
            await events.poll_once(source.id)
            async def failed_ack(*args, **kwargs):
                raise MCPFailure("transport", "synthetic_ack_failure")
            monkeypatch.setattr(events, "acknowledge", failed_ack)
            with pytest.raises(MCPFailure):
                await service.consume_once(reg.id)
            assert (await service.listing("local"))["total"] == 1
            await service.close()
            await events.close()
            events = runtime(gate, source, tmp_path, clock)
            service = notifications(gate, events, (reg,), tmp_path, clock)
            await service.consume_once(reg.id)
            assert (await service.listing("local"))["total"] == 1
            assert events.store.consumer(source.id, "notification:" + reg.id)["position"] == 11
        finally:
            await service.close()
            await events.close()


@pytest.mark.anyio
async def test_real_shared_action_policy_never_calls_llm_for_registered_references(tmp_path):
    from app.addon_action.egress import ActionEgress
    from app.addon_action.models import ApprovalKey, ExecutionScene, OperationGroup, Permission
    from app.addon_action.policy import ActionPolicy
    from app.addon_action.store import ActionStore
    from app.external_mcp.reference_egress import reference_arguments_allowed
    clock = Clock()
    async with connected(tmp_path / "provider") as (control, gate, _):
        source = profile(gate)
        events = runtime(gate, source, tmp_path, clock)
        # classifierを用意しない実ActionEgress。通常経路の非空引数は引き続き拒否される。
        policy = ActionPolicy(ActionStore(tmp_path / "actions.db"), events.reader.sanitizer,
                              egress=ActionEgress(None).allowed, reference_egress=reference_arguments_allowed)
        gate.confirmations = policy
        key = ApprovalKey(source.connection_id, gate.registry.entry(source.connection_id).connection.identity,
                          OperationGroup.NORMAL, ExecutionScene.AUTONOMOUS)
        reg = registration(gate)
        service = notifications(gate, events, (reg,), tmp_path, clock)
        policy.store.set_permission(key, Permission.UNAPPROVED)
        try:
            with pytest.raises(MCPFailure, match="background_read_not_approved"):
                await service.consume_once(reg.id)
            policy.store.set_permission(key, Permission.ALWAYS)
            await service.consume_once(reg.id)
            control.update(position=11)
            clock.advance()
            await events.poll_once(source.id)
            await service.consume_once(reg.id)
            row = (await service.listing("local"))["items"][0]
            assert (await service.detail(row["id"], "local"))["state"] == "available"
            with pytest.raises(MCPFailure, match="egress_privacy_blocked"):
                await policy.validate_egress({"task_ref": "task-11"})
            for arguments in ({"text": "外部へ送る本文です"}, {"resource": "https://private.invalid"}, {"token": "synthetic-secret"}, {"nested": {"task": "a"}}):
                with pytest.raises(MCPFailure, match="egress_privacy_blocked"):
                    await policy.validate_reference_egress(arguments)
            policy.store.set_permission(key, Permission.DENIED)
            with pytest.raises(MCPFailure):
                await service.detail(row["id"], "local")
        finally:
            await service.close()
            await events.close()


@pytest.mark.anyio
async def test_registered_binding_does_not_follow_conversation_selection(tmp_path):
    from app.tool_use.binding import BindingTarget
    clock = Clock()
    targets = tuple(BindingTarget(name, "external-test", "miori", name, ("snapshot", "history", "detail"),
                                  encode({"target": name})) for name in ("repo-A", "repo-B"))
    async with connected(tmp_path / "provider", binding=True, bindings=targets) as (control, gate, _):
        source = profile(gate, binding_id="repo-A", arguments=encode({"target": "repo-A"}))
        events = runtime(gate, source, tmp_path, clock)
        reg = registration(gate, binding_id="repo-A", arguments_json=encode({"target": "repo-A"}))
        service = notifications(gate, events, (reg,), tmp_path, clock)
        try:
            await service.consume_once(reg.id)
            control.update(position=11, expected_target="repo-A")
            clock.advance()
            await events.poll_once(source.id)
            await service.consume_once(reg.id)
            row = (await service.listing("local"))["items"][0]
            gate.bindings.resolve("miori", "current-conversation", source.connection_id, "detail", explicit="repo-B")
            assert (await service.detail(row["id"], "local"))["state"] == "available"
            gate.bindings.targets = (replace(targets[0], arguments_json=encode({"target": "repo-B"})), targets[1])
            with pytest.raises(MCPFailure):
                await service.detail(row["id"], "local")
        finally:
            await service.close()
            await events.close()


@pytest.mark.anyio
async def test_detail_deadline_and_output_are_finite(tmp_path):
    clock = Clock()
    async with connected(tmp_path / "provider") as (control, gate, _):
        source = profile(gate)
        events = runtime(gate, source, tmp_path, clock)
        reg = registration(gate)
        service = notifications(gate, events, (reg,), tmp_path, clock, Limits(read_bytes=256))
        try:
            await service.consume_once(reg.id)
            control.update(position=11, detail_text="合成テキスト" * 300)
            clock.advance()
            await events.poll_once(source.id)
            await service.consume_once(reg.id)
            row = (await service.listing("local"))["items"][0]
            detail = await service.detail(row["id"], "local")
            assert detail["state"] == "available" and detail["omitted"]
            assert len(detail["text"].encode()) <= 256
            service.store.limits = replace(service.store.limits, read_seconds=.02)
            control.update(delay=.1)
            assert (await service.detail(row["id"], "local"))["state"] == "unavailable"
            assert service.store.get(row["id"], "local")["state"] == "unread"
        finally:
            await service.close()
            await events.close()


@pytest.mark.anyio
async def test_registered_resource_detail_and_current_revision(tmp_path):
    clock = Clock()
    async with connected(tmp_path / "provider") as (control, gate, _):
        source = profile(gate)
        events = runtime(gate, source, tmp_path, clock)
        entry = gate.registry.entry(source.connection_id)
        resource = next(r for r in (entry.active or entry.staged).document["resources"] if r["uri"] == "events://fixture/detail")
        reg = registration(gate, detail=Operation("resource", resource["uri"], digest(resource)), reference_arguments_json="{}")
        service = notifications(gate, events, (reg,), tmp_path, clock)
        try:
            await service.consume_once(reg.id)
            control.update(position=11, detail_references={"task_ref": "task-11", "resource_ref": "safe-ref", "revision": "newer"})
            clock.advance()
            await events.poll_once(source.id)
            await service.consume_once(reg.id)
            row = (await service.listing("local"))["items"][0]
            detail = await service.detail(row["id"], "local")
            assert detail["state"] == "available" and detail["revision"] == "newer"
            assert detail["acquired_at"] == clock() and detail["untrusted"]
            events.store.set_status(source.id, "stopped", "contract")
            assert (await service.listing("local"))["sources"][0]["status"] == "unavailable"
        finally:
            await service.close()
            await events.close()

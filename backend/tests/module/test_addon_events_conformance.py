"""#190: Coreをimportしない別process MCPによる公開契約の適合検証。"""
import asyncio
import json
import logging
import os
from dataclasses import replace

import pytest

from app.addon_events.contracts import Limits
from app.external_mcp import ExecutionContext, MCPFailure
from app.tool_use.binding import BindingTarget
from tests.addon_events_test_support import Clock, connected, profile, runtime, until


@pytest.mark.parametrize("transport", ["stdio", "streamable_http"])
@pytest.mark.parametrize("kind", ["tool", "resource"])
def test_baseline_shared_ingestion_restart_replay_and_gap(tmp_path, transport, kind, caplog):
    caplog.set_level(logging.DEBUG)
    async def run():
        clock = Clock()
        async with connected(tmp_path, transport) as (control, gate, client):
            source = profile(gate, kind=kind, limits=Limits(max_events=2))
            service = runtime(gate, source, tmp_path, clock)
            try:
                await service.subscribe(source.id, "fast", source.context)
                await service.subscribe(source.id, "slow", source.context)
                initial = await service.read(source.id, "fast", source.context)
                assert initial.events == () and initial.snapshot == {"count": 10, "status": "current"}
                await service.acknowledge(source.id, "fast", initial.receipt, source.context)
                control.update(position=12, secret="synthetic-secret")
                await service.poll_once(source.id)
                assert control.calls("history") == 1
                fast = await service.read(source.id, "fast", source.context)
                assert [e["position"] for e in fast.events] == [11, 12]
                assert all("resource_ref" not in e["metadata"] for e in fast.events)
                await service.acknowledge(source.id, "fast", fast.receipt, source.context)
                clock.advance()
                control.update(position=14)
                await service.poll_once(source.id)
                assert service.store.consumer(source.id, "slow")["position"] == 10
                assert service.diagnostics()[0]["buffer_count"] == 2
                slow = await service.read(source.id, "slow", source.context)
                assert [e["position"] for e in slow.events] == [11, 12, 13, 14]
                assert control.calls("history") == 3  # 通常取得2回＋遅いconsumerのreplay1回
                assert service.store.consumer(source.id, "fast")["position"] == 12
                # ack前にBackendを終了。処理済みとは読み替えず、再起動後も再配送する。
            finally:
                await service.close()
            service = runtime(gate, source, tmp_path, clock)
            try:
                assert service.store.consumer(source.id, "slow")["position"] == 10
                fast = await service.read(source.id, "fast", source.context)
                assert [e["position"] for e in fast.events] == [13, 14]
                control.update(floor=13, position=15)
                missing = await service.read(source.id, "slow", source.context)
                assert missing.events == () and missing.gaps[0]["reason"] == "history_unavailable"
                assert missing.snapshot["count"] == 15
                with pytest.raises(MCPFailure, match="event_gap_ack_required"):
                    await service.acknowledge(source.id, "slow", missing.receipt, source.context)
                assert service.store.consumer(source.id, "slow")["position"] == 10
                await service.acknowledge(source.id, "slow", missing.receipt, source.context, accept_gap=True)
                assert service.store.consumer(source.id, "slow")["gap_count"] == 1
                # 取得側にも期限切れ。安全なsnapshot位置へ自動再開し、その後のEventを取得。
                clock.advance()
                control.update(floor=15, position=16)
                await service.poll_once(source.id)
                assert service.store.state(source.id)["position"] == 16
                assert service.store.state(source.id)["gap_count"] == 1
                clock.advance()
                control.update(position=17)
                await service.poll_once(source.id)
                assert service.store.state(source.id)["position"] == 17
                await service.unsubscribe(source.id, "slow", source.context)
                calls = control.calls("history")
                clock.advance()
                control.update(position=18)
                await service.poll_once(source.id)
                assert control.calls("history") == calls + 1
                await service.unsubscribe(source.id, "fast", source.context)
                clock.advance()
                control.update(position=19)
                await service.poll_once(source.id)
                assert control.calls("history") == calls + 1
                assert service.diagnostics()[0]["consumer_count"] == 0
            finally:
                await service.close()
        assert "synthetic-private-body" not in caplog.text
        assert "synthetic-secret" not in caplog.text
        assert "https://private.invalid" not in caplog.text
        assert b"synthetic-private-body" not in (tmp_path / "events.sqlite3").read_bytes()
        assert b"synthetic-secret" not in (tmp_path / "events.sqlite3").read_bytes()
    asyncio.run(run())
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("mode,expected", [
    ("duplicate", "ready"), ("stale", "ready"), ("invalid_event", "ready"),
    ("unsafe_cursor", "stopped"), ("missing_position", "stopped"),
    ("id_conflict", "stopped"), ("position_conflict", "stopped"),
])
def test_bad_events_safe_skip_and_stop(tmp_path, mode, expected):
    async def run():
        async with connected(tmp_path) as (control, gate, client):
            source = profile(gate)
            service = runtime(gate, source, tmp_path)
            try:
                await service.subscribe(source.id, "consumer", source.context)
                control.update(position=12, mode=mode)
                await service.poll_once(source.id)
                assert service.store.state(source.id)["status"] == expected
                if expected == "stopped":
                    assert service.store.state(source.id)["position"] == 10
                    assert service.diagnostics()[0]["buffer_count"] == 0
                    control.update(mode=None)
                    await service.resume(source.id)
                    await service.poll_once(source.id)
                    assert service.store.state(source.id)["position"] == 12
                else:
                    result = await service.read(source.id, "consumer", source.context)
                    if mode == "invalid_event":
                        assert [e["position"] for e in result.events] == [12]
                        assert result.gaps == ({"reason": "invalid_event", "position": 11},)
                    else:
                        assert [e["position"] for e in result.events] == [11, 12]
            finally:
                await service.close()
    asyncio.run(run())


def test_authority_revocation_definition_egress_and_buffer_privacy(tmp_path):
    async def run():
        async with connected(tmp_path) as (control, gate, client):
            from app.tool_use.projection import Sanitizer
            from tests.tool_use_test_support import Scanner
            secrets = []
            sanitizer = Sanitizer(Scanner(), dynamic_private_values=lambda: tuple(secrets))
            source = profile(gate)
            service = runtime(gate, source, tmp_path, sanitizer=sanitizer)
            try:
                await service.subscribe(source.id, "consumer", source.context)
                control.update(position=11, secret="later-secret")
                await service.poll_once(source.id)
                delivery = await service.read(source.id, "consumer", source.context)
                gate.registry.set_enabled(source.connection_id, False)
                with pytest.raises(MCPFailure):
                    await service.read(source.id, "consumer", source.context)
                with pytest.raises(MCPFailure):
                    await service.acknowledge(source.id, "consumer", delivery.receipt, source.context)
                gate.registry.set_enabled(source.connection_id, True)
                gate.registry.stage(source.connection_id, await client.discover())
                # 再検査で新たに秘密扱いとなった既存bufferも配送しない。
                secrets.append("later-secret")
                delivery = await service.read(source.id, "consumer", source.context)
                assert not delivery.events and delivery.gaps[0]["reason"] == "privacy_changed"
                other = replace(source.context, character_id="other")
                with pytest.raises(MCPFailure, match="event_consumer_identity_changed"):
                    await service.read(source.id, "consumer", other)
                control.update(definition=2)
                gate.registry.stage(source.connection_id, await client.discover())
                with pytest.raises(MCPFailure, match="event_definition_changed"):
                    await service.read(source.id, "consumer", source.context)
                assert service.store.consumer(source.id, "consumer")["position"] == 10
            finally:
                await service.close()
    asyncio.run(run())


def test_scoped_binding_sharing_and_untrusted_denied(tmp_path):
    async def run():
        async with connected(tmp_path / "untrusted", trusted=False) as (control, gate, client):
            source = profile(gate)
            service = runtime(gate, source, tmp_path / "untrusted")
            try:
                with pytest.raises(MCPFailure, match="event_requires_trusted_read"):
                    await service.subscribe(source.id, "consumer", source.context)
                assert control.calls("snapshot") == 0
            finally:
                await service.close()
        target = BindingTarget("bound", "external-test", "miori", "対象", ("snapshot", "history"), '{"target":"one"}')
        async with connected(tmp_path / "binding", bindings=(target,), binding=True,
                             sharing={"mode": "character_bound", "character_id": "miori"}) as (control, gate, client):
            source = profile(gate, binding_id="bound", arguments='{"target":"one"}')
            service = runtime(gate, source, tmp_path / "binding")
            try:
                await service.subscribe(source.id, "consumer", source.context)
                control.update(position=11)
                await service.poll_once(source.id)
                gate.bindings.targets = ()
                with pytest.raises(MCPFailure):
                    await service.read(source.id, "consumer", source.context)
                assert service.store.consumer(source.id, "consumer")["position"] == 10
            finally:
                await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("transport", ["stdio", "streamable_http"])
def test_real_wake_and_missing_wake_poll_and_shutdown(tmp_path, transport):
    async def run():
        async with connected(tmp_path, transport) as (control, gate, client):
            source = profile(gate, wake=True)
            clock = Clock()
            service = runtime(gate, source, tmp_path, clock)
            try:
                await service.subscribe(source.id, "consumer", source.context)
                service.start()
                await until(lambda: source.id in service._watches)
                await asyncio.sleep(.2)
                # listen開始直後の再確認を先に消化し、実際のResource更新だけで次を起こす。
                clock.advance(1)
                await until(lambda: control.calls("history") >= 2)
                await asyncio.sleep(.1)
                assert service.store.state(source.id)["position"] == 10
                initial_calls = control.calls("history")
                control.update(position=11, wake=True)
                clock.advance(1)
                await until(lambda: service.store.state(source.id)["position"] == 11)
                assert control.calls("history") > initial_calls
                # pushを落としても、仮想時計で既定60秒後の定期取得が回復する。
                control.update(position=12, wake=False)
                clock.advance(60)
                await until(lambda: service.store.state(source.id)["position"] == 12)
                await service.unsubscribe(source.id, "consumer", source.context)
                assert source.id not in service._watches
                before = control.calls("history")
                control.update(position=13, wake=True)
                clock.advance(60)
                await asyncio.sleep(.1)
                assert control.calls("history") == before
            finally:
                await service.close()
            assert all(task.done() for task in service._workers)
    asyncio.run(run())


@pytest.mark.parametrize("limit_kind", ["time", "bytes"])
def test_retention_limits_replay_and_shared_progress(tmp_path, limit_kind):
    async def run():
        async with connected(tmp_path) as (control, gate, client):
            clock = Clock()
            limits = Limits(max_bytes=512, max_event_bytes=256) if limit_kind == "bytes" else Limits()
            source = profile(gate, limits=limits)
            service = runtime(gate, source, tmp_path, clock)
            try:
                await service.subscribe(source.id, "consumer", source.context)
                control.update(position=12)
                await service.poll_once(source.id)
                if limit_kind == "time":
                    clock.advance(72 * 3600 - 1)
                    service.store.prune(source)
                    assert service.diagnostics()[0]["buffer_count"] == 2
                    clock.advance(1)
                    service.store.prune(source)
                    assert service.diagnostics()[0]["buffer_count"] == 0
                else:
                    assert service.diagnostics()[0]["buffer_bytes"] <= 512
                    assert service.diagnostics()[0]["buffer_count"] < 2
                assert service.store.consumer(source.id, "consumer")["position"] == 10
                replay = await service.read(source.id, "consumer", source.context)
                assert [e["position"] for e in replay.events] == [11, 12]
                assert replay.recovery == {"reason": "buffer_unavailable", "after": 10, "replayed": True}
                assert service.diagnostics()[0]["buffer_bytes"] <= limits.max_bytes
            finally:
                await service.close()
    asyncio.run(run())


def test_atomic_commit_failure_backend_restart_and_reconnect(tmp_path, monkeypatch):
    async def run():
        clock = Clock()
        async with connected(tmp_path) as (control, gate, client):
            source = profile(gate)
            service = runtime(gate, source, tmp_path, clock)
            await service.subscribe(source.id, "consumer", source.context)
            control.update(position=12)
            original = service.store._prune
            def crash(source, db):
                original(source, db)
                raise RuntimeError("injected-crash-before-commit")
            monkeypatch.setattr(service.store, "_prune", crash)
            # poll開始時のpruneは成功し、buffer INSERT/cursor更新後の同一TX内で障害を入れる。
            monkeypatch.setattr(service.store, "prune", lambda source: None)
            try:
                with pytest.raises(RuntimeError, match="injected-crash"):
                    await service.poll_once(source.id)
                assert service.store.state(source.id)["position"] == 10
                assert service.diagnostics()[0]["buffer_count"] == 0
            finally:
                await service.close()
            # MCP session自体も切断・再接続。永続consumerとcursorを再利用する。
        async with connected(tmp_path) as (control, gate, client):
            control.update(position=14)
            service = runtime(gate, source, tmp_path, clock)
            try:
                await service.poll_once(source.id)
                delivery = await service.read(source.id, "consumer", source.context)
                assert [e["position"] for e in delivery.events] == [11, 12, 13, 14]
                await service.acknowledge(source.id, "consumer", delivery.receipt, source.context)
                assert service.store.consumer(source.id, "consumer")["position"] == 14
            finally:
                await service.close()
    asyncio.run(run())


def test_backoff_unavailable_degraded_budget_snapshot_pending(tmp_path):
    async def run():
        clock = Clock()
        async with connected(tmp_path) as (control, gate, client):
            source = profile(gate)
            service = runtime(gate, source, tmp_path, clock)
            try:
                await service.subscribe(source.id, "consumer", source.context)
                control.update(position=12, mode="error")
                await service.poll_once(source.id)
                before = control.calls("history")
                for _ in range(5):
                    await service.poll_once(source.id)
                assert control.calls("history") == before
                assert service.store.state(source.id)["status"] == "backoff"
                clock.advance(5)
                control.update(mode=None)
                await service.poll_once(source.id)
                assert service.store.state(source.id)["position"] == 12
                gate.registry.availability(source.connection_id, "unavailable")
                clock.advance()
                before = control.calls("history")
                await service.poll_once(source.id)
                assert control.calls("history") == before
                with pytest.raises(MCPFailure):
                    await service.read(source.id, "consumer", source.context)
                # 外部MCPにはdegraded広告がないため、将来のhealthy集合をEntry境界へ注入する。
                entry = gate.registry.entry(source.connection_id)
                entry.availability = "degraded"
                entry.healthy_operations = frozenset({"tool:snapshot"})
                clock.advance()
                await service.poll_once(source.id)
                assert control.calls("history") == before
                entry.healthy_operations = frozenset({"tool:snapshot", "tool:history"})
                clock.advance()
                await service.poll_once(source.id)
                assert control.calls("history") == before + 1
                entry.availability = "available"
                control.update(floor=13, position=14, mode="broken_snapshot")
                clock.advance()
                await service.poll_once(source.id)
                assert service.store.state(source.id)["position"] == 12
                assert service.store.state(source.id)["status"] == "backoff"
                control.update(mode=None)
                clock.advance(5)
                await service.poll_once(source.id)
                assert service.store.state(source.id)["position"] == 14
                # 通常callと同じ共有budgetを消費し、loopを作り直しても制限を回避できない。
                for i in range(35):
                    clock.advance()
                    await service.poll_once(source.id)
                assert service.store.state(source.id)["status"] == "backoff"
                assert control.calls("history") < 35
            finally:
                await service.close()
    asyncio.run(run())


def test_late_subscription_and_epoch_reset(tmp_path):
    async def run():
        async with connected(tmp_path) as (control, gate, client):
            clock = Clock()
            source = profile(gate)
            service = runtime(gate, source, tmp_path, clock)
            try:
                await service.subscribe(source.id, "early", source.context)
                control.update(position=12)
                await service.subscribe(source.id, "late", source.context)
                await service.poll_once(source.id)
                assert [e["position"] for e in (await service.read(source.id, "early", source.context)).events] == [11, 12]
                late = await service.read(source.id, "late", source.context)
                assert not late.events and late.snapshot["count"] == 12
                control.update(epoch="era2", position=3)
                clock.advance()
                await service.poll_once(source.id)
                early = await service.read(source.id, "early", source.context)
                late = await service.read(source.id, "late", source.context)
                assert early.gaps[0]["epoch_changed"] and late.gaps[0]["epoch_changed"]
                await service.acknowledge(source.id, "early", early.receipt, source.context, accept_gap=True)
                assert service.store.consumer(source.id, "late")["epoch"] == "era1"
                with pytest.raises(MCPFailure, match="stale_event_receipt"):
                    await service.acknowledge(source.id, "early", early.receipt, source.context, accept_gap=True)
            finally:
                await service.close()
    asyncio.run(run())


def test_background_tool_permission_and_egress_are_rechecked(tmp_path):
    async def run():
        from app.addon_action.policy import ActionPolicy
        from app.addon_action.store import ActionStore
        from app.addon_action.models import ApprovalKey, OperationGroup, ExecutionScene, Permission
        from app.tool_use.projection import Sanitizer
        from tests.tool_use_test_support import Scanner
        allowed = True
        async def egress(arguments):
            return allowed
        async with connected(tmp_path) as (control, gate, client):
            source = profile(gate)
            policy = ActionPolicy(ActionStore(tmp_path / "actions.sqlite3"), Sanitizer(Scanner()), egress=egress)
            gate.confirmations = policy
            identity = gate.registry.entry(source.connection_id).connection.identity
            key = ApprovalKey(source.connection_id, identity, OperationGroup.NORMAL, ExecutionScene.AUTONOMOUS)
            policy.store.set_permission(key, Permission.UNAPPROVED)
            service = runtime(gate, source, tmp_path)
            try:
                with pytest.raises(MCPFailure, match="background_read_not_approved"):
                    await service.subscribe(source.id, "consumer", source.context)
                assert control.calls("snapshot") == 0
                policy.store.set_permission(key, Permission.ALWAYS)
                await service.subscribe(source.id, "consumer", source.context)
                control.update(position=11)
                await service.poll_once(source.id)
                policy.store.set_permission(key, Permission.DENIED)
                with pytest.raises(MCPFailure, match="background_read_not_approved"):
                    await service.read(source.id, "consumer", source.context)
                policy.store.set_permission(key, Permission.ALWAYS)
                allowed = False
                with pytest.raises(MCPFailure, match="egress_privacy_blocked"):
                    await service.read(source.id, "consumer", source.context)
                assert service.store.consumer(source.id, "consumer")["position"] == 10
            finally:
                await service.close()
    asyncio.run(run())

def test_retention_boundary_uses_one_consistent_window(tmp_path, monkeypatch):
    """期限の直前・直後を同じread内で混ぜ、間のEventを正常処理扱いにしない。"""
    async def run():
        async with connected(tmp_path) as (control, gate, client):
            clock = Clock()
            source = profile(gate)
            service = runtime(gate, source, tmp_path, clock)
            try:
                await service.subscribe(source.id, "consumer", source.context)
                control.update(position=11)
                await service.poll_once(source.id)
                clock.advance(1)
                control.update(position=12)
                await service.poll_once(source.id)
                clock.advance(72 * 3600 - 2)
                def crossing_clock():
                    observed = clock()
                    clock.advance(1)
                    return observed
                monkeypatch.setattr(service.store, "clock", crossing_clock)
                delivery = await service.read(source.id, "consumer", source.context)
                assert [event["position"] for event in delivery.events] == [11, 12]
                assert service.store.consumer(source.id, "consumer")["position"] == 10
                monkeypatch.setattr(service.store, "clock", clock)
                # 次のreadでは期限切れを正式に判定し、source replayを明示する。
                replay = await service.read(source.id, "consumer", source.context)
                assert replay.recovery["reason"] == "buffer_unavailable"
                assert [event["position"] for event in replay.events] == [11, 12]
            finally:
                await service.close()
    asyncio.run(run())

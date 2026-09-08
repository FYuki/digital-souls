"""#192の設定復元、health、実行境界と公開APIを検証する。"""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from app.addon_admin.runtime import AddonRuntime, HealthPolicy
from app.addon_admin.store import SettingsStore
from app.external_mcp import (
    Connection,
    ExecutionContext,
    ExecutionGate,
    Registry,
    MCPFailure,
)
from app.external_mcp.models import encode
from app.routers.addon_admin import router
from tests.external_mcp_test_support import FakeSource, manifest

CTX = ExecutionContext("miori", "admin-tests")


def setup(tmp_path, *, enabled=True, self_owned=False):
    value = manifest(trusted=True, transport="streamable_http")
    value["core_policy"]["enabled"] = enabled
    if self_owned:
        # #221の接続factoryを代用する管理側fixture。公開Manifest受付は拡張しない。
        value["connection"]["ownership"] = "self_owned"
        connection = Connection(encode(value))
    else:
        connection = Connection.from_manifest(value)
    registry = Registry()
    registry.register(connection)
    gate = ExecutionGate(registry)
    runtime = AddonRuntime(gate, settings_path=tmp_path / "addon-settings.json")
    return connection, gate, runtime


def test_settings_migrate_once_restart_last_write_wins_and_no_metadata_loss(tmp_path):
    connection, gate, runtime = setup(tmp_path, enabled=False)
    original = connection.manifest
    assert runtime.list()[0]["effective_state"] == "disabled"
    runtime.set_enabled(connection.id, True)
    runtime.set_enabled(connection.id, False)
    runtime.set_enabled(connection.id, True)
    _, _, restored = setup(tmp_path, enabled=False)
    assert restored.list()[0]["desired_enabled"] is True
    assert restored.list()[0]["availability"] == "unknown"
    assert gate.registry.entry(connection.id).connection.manifest == original
    stored = json.loads((tmp_path / "addon-settings.json").read_text())
    assert stored == {"version": 1, "users": {"local": {connection.id: True}}}


def test_failed_save_keeps_confirmed_setting(tmp_path, monkeypatch):
    connection, gate, runtime = setup(tmp_path)

    def fail(*_):
        raise OSError("private endpoint and credential")

    monkeypatch.setattr("app.addon_admin.store.os.replace", fail)
    with pytest.raises(OSError):
        runtime.set_enabled(connection.id, False)
    assert runtime.list()[0]["desired_enabled"] is True
    assert (
        SettingsStore(tmp_path / "addon-settings.json").initial(connection.id, False)
        is True
    )


def test_api_lists_registered_unknown_disabled_unavailable_and_redacts(tmp_path):
    async def run():
        connection, gate, runtime = setup(tmp_path)
        app = FastAPI()
        app.include_router(router)
        app.state.addon_manager = runtime
        gate.registry.availability(
            connection.id, "unavailable", error_code="raw-secret"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/addon-admin/connections")
            assert response.headers["cache-control"] == "no-store"
            row = response.json()[0]
            assert row["effective_state"] == "unavailable"
            assert row["error_code"] == "connection_failed"
            assert row["last_checked_at"]
            assert set(row) == {
                "connection_instance_id",
                "display_name",
                "source_type",
                "desired_enabled",
                "availability",
                "effective_state",
                "error_code",
                "last_checked_at",
            }
            response = await client.patch(
                f"/addon-admin/connections/{connection.id}",
                json={"desired_enabled": False},
            )
            assert response.json()["effective_state"] == "disabled"
            assert response.json()["availability"] == "unavailable"
            for bad in (
                {"desired_enabled": "false"},
                {"desired_enabled": True, "raw-secret": "private"},
            ):
                response = await client.patch(
                    f"/addon-admin/connections/{connection.id}", json=bad
                )
                assert response.status_code == 422
                assert "raw-secret" not in response.text
                assert "private" not in response.text
            response = await client.patch(
                "/addon-admin/connections/absent", json={"desired_enabled": True}
            )
            assert response.status_code == 404

    asyncio.run(run())


def test_off_invalidates_queued_dispatch_and_resume_even_after_on(tmp_path):
    async def run():
        connection, gate, runtime = setup(tmp_path)
        source = FakeSource(connection)
        entered, release = asyncio.Event(), asyncio.Event()
        original = source.call_tool

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        source.call_tool = slow
        gate.parallelism = 1
        async with gate.attach(connection.id, source):
            loop = gate.begin_loop(CTX)
            started = asyncio.create_task(
                gate.invoke(connection.id, "native-tool", {"value": 1}, loop)
            )
            await entered.wait()
            queued = asyncio.create_task(
                gate.invoke(connection.id, "native-tool", {"value": 2}, loop)
            )
            await asyncio.sleep(0)
            snapshot = gate.registry.entry(connection.id).active
            runtime.set_enabled(connection.id, False)
            assert not gate.catalog_snapshots(loop)
            runtime.set_enabled(connection.id, True)
            assert runtime.list()[0]["availability"] == "unknown"
            release.set()
            assert (await started)["outcome"] == "succeeded"
            assert (await queued)["outcome"] == "failed"
            assert len(source.calls) == 1
            await gate.refresh(connection.id)
            assert gate.registry.entry(connection.id).active is snapshot
            gate.end_loop(loop)
            loop = gate.begin_loop(CTX)
            source.result = {
                "resultType": "input_required",
                "inputRequests": {"input": {"method": "elicitation/create"}},
            }
            result = await gate.invoke(connection.id, "native-tool", {"value": 1}, loop)
            assert result["outcome"] == "input_required"
            runtime.set_enabled(connection.id, False)
            runtime.set_enabled(connection.id, True)
            await gate.refresh(connection.id)
            with pytest.raises(MCPFailure, match="invalid_interaction"):
                await gate.resume(result["interaction_id"], {"input": {}}, loop)
            gate.end_loop(loop)

    asyncio.run(run())


def test_external_never_degraded_and_single_operation_failure_keeps_available(tmp_path):
    async def run():
        connection, gate, runtime = setup(tmp_path)
        source = FakeSource(connection)
        async with gate.attach(connection.id, source):
            loop = gate.begin_loop(CTX)
            source.failures = [
                MCPFailure("transport", "timeout"),
                MCPFailure("transport", "timeout"),
            ]
            await gate.invoke(connection.id, "native-tool", {"value": 1}, loop)
            assert runtime.list()[0]["availability"] == "available"
            with pytest.raises(ValueError):
                gate.registry.availability(connection.id, "degraded")
            gate.end_loop(loop)

    asyncio.run(run())


def test_self_owned_partial_health_allows_only_healthy_reads(tmp_path):
    async def run():
        connection, gate, runtime = setup(tmp_path, self_owned=True)
        source = FakeSource(connection)
        async with gate.attach(connection.id, source):
            gate.registry.availability(
                connection.id,
                "degraded",
                healthy_operations=frozenset({"tool:native-tool"}),
            )
            loop = gate.begin_loop(CTX)
            assert runtime.list()[0]["source_type"] == "self_owned"
            assert (
                await gate.invoke(connection.id, "native-tool", {"value": 1}, loop)
            )["outcome"] == "succeeded"
            assert (await gate.read_resource(connection.id, "test://resource", loop))[
                "outcome"
            ] == "failed"
            source.data.tools[0]["annotations"]["readOnlyHint"] = False
            await gate.refresh(connection.id)
            gate.end_loop(loop)
            gate.registry.availability(
                connection.id,
                "degraded",
                healthy_operations=frozenset({"tool:native-tool"}),
            )
            loop = gate.begin_loop(CTX)
            assert (
                await gate.invoke(connection.id, "native-tool", {"value": 1}, loop)
            )["outcome"] == "failed"
            gate.end_loop(loop)

    asyncio.run(run())


class HealthClient(FakeSource):
    connection_failure = None

    def __init__(self, connection):
        super().__init__(connection)
        self.health_calls = 0
        self.health_error = None
        self.health_permits = None
        self.closed = False

    @asynccontextmanager
    async def connect(self):
        try:
            yield self
        finally:
            self.closed = True

    async def health(self):
        if self.health_permits is not None:
            await self.health_permits.acquire()
        self.health_calls += 1
        if self.health_error:
            raise self.health_error


async def eventually(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def test_health_threshold_recovery_disabled_session_and_teardown(tmp_path):
    async def run():
        connection, gate, _ = setup(tmp_path)
        client = HealthClient(connection)
        policy = HealthPolicy(interval=0.04, timeout=2, disconnect_poll=0.005)
        runtime = AddonRuntime(gate, policy=policy, client_factory=lambda _: client)
        await runtime.start()
        try:
            await eventually(lambda: runtime.list()[0]["availability"] == "available")
            before_failure = client.health_calls
            client.health_permits = permits = asyncio.Semaphore(0)
            client.health_error = TimeoutError()
            permits.release()
            await eventually(lambda: client.health_calls >= before_failure + 1)
            assert runtime.list()[0]["availability"] == "available"
            permits.release()
            permits.release()
            await eventually(lambda: client.health_calls >= before_failure + 3)
            assert runtime.list()[0]["availability"] == "unavailable"
            assert runtime.list()[0]["error_code"] == "health_check_failed"
            client.health_error = None
            permits.release()
            client.health_permits = None
            await eventually(lambda: runtime.list()[0]["availability"] == "available")
            runtime.set_enabled(connection.id, False)
            await asyncio.sleep(0.06)
            assert not client.closed
            before = client.health_calls
            await asyncio.sleep(0.06)
            assert client.health_calls == before
            runtime.set_enabled(connection.id, True)
            assert runtime.list()[0]["availability"] == "unknown"
            await eventually(lambda: runtime.list()[0]["availability"] == "available")
        finally:
            await runtime.close()
        assert client.closed
        assert not runtime._tasks

    asyncio.run(run())


@pytest.mark.parametrize(
    "category, code",
    [("auth", "authentication_failed"), ("protocol", "protocol_error")],
)
def test_definite_health_error_immediate(tmp_path, category, code):
    async def run():
        connection, gate, _ = setup(tmp_path)
        client = HealthClient(connection)
        runtime = AddonRuntime(
            gate,
            policy=HealthPolicy(interval=0.02, disconnect_poll=0.005),
            client_factory=lambda _: client,
        )
        await runtime.start()
        try:
            await eventually(lambda: runtime.list()[0]["availability"] == "available")
            client.health_error = MCPFailure(category, "private-error")
            await eventually(lambda: client.health_calls >= 1)
            assert runtime.list()[0]["availability"] == "unavailable"
            assert runtime.list()[0]["error_code"] == code
            assert runtime.list()[0]["desired_enabled"] is True
        finally:
            await runtime.close()

    asyncio.run(run())


def test_off_during_question_generation_cannot_create_stale_waiting():
    from tests.unit.test_tool_use import (
        runtime as tool_runtime,
        Decisions,
        InputSource,
        call,
    )
    from app.tool_use.routing import ToolDecision

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowDecision(Decisions):
            async def decide(self, context, cancellation):
                if context.get("pending"):
                    entered.set()
                    await release.wait()
                return await super().decide(context, cancellation)

        decisions = SlowDecision(call, ToolDecision("clarify", instruction="色は？"))
        async with tool_runtime(decisions, source_type=InputSource) as (
            service,
            source,
            gate,
        ):
            admin = AddonRuntime(gate)
            admin.on_disabled = service.connection_disabled
            task = asyncio.create_task(service.run("miori", "session", "操作して"))
            await asyncio.wait_for(entered.wait(), 2)
            admin.set_enabled(source.connection.id, False)
            admin.set_enabled(source.connection.id, True)
            await gate.refresh(source.connection.id)
            release.set()
            result = await task
            assert not result.waiting
            assert not gate._pending and not gate._loops
            assert len(source.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("failure_point", ["os.replace", "fsync_directory"])
def test_api_save_failure_is_sanitized(tmp_path, monkeypatch, failure_point):
    async def run():
        connection, _, runtime = setup(tmp_path)
        app = FastAPI()
        app.include_router(router)
        app.state.addon_manager = runtime

        def fail(*_):
            raise OSError("private endpoint and credential")

        monkeypatch.setattr(f"app.addon_admin.store.{failure_point}", fail)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.patch(
                f"/addon-admin/connections/{connection.id}",
                json={"desired_enabled": False},
            )
        assert response.status_code == 503
        replaced = failure_point == "fsync_directory"
        assert response.json()["detail"] == (
            "settings_durability_uncertain" if replaced else "settings_save_failed"
        )
        assert "private endpoint and credential" not in response.text
        assert runtime.list()[0]["desired_enabled"] is (not replaced)
        restored = SettingsStore(tmp_path / "addon-settings.json")
        assert restored.initial(connection.id, True) is (not replaced)

    asyncio.run(run())


def test_settings_syncs_parent_after_replacement(tmp_path, monkeypatch):
    connection, _, runtime = setup(tmp_path)
    synchronized = []

    def sync(parent):
        synchronized.append(parent)
        stored = json.loads((parent / "addon-settings.json").read_text())
        assert stored["users"]["local"][connection.id] is False

    monkeypatch.setattr("app.addon_admin.store.fsync_directory", sync)
    runtime.set_enabled(connection.id, False)
    assert synchronized == [tmp_path]


@pytest.mark.parametrize("transition", ["reconnect", "enable"])
def test_rechecking_clears_previous_partial_health(tmp_path, transition):
    connection, gate, runtime = setup(tmp_path, self_owned=True)
    gate.registry.availability(
        connection.id, "degraded", healthy_operations=frozenset({"tool:read"})
    )
    if transition == "reconnect":
        gate.registry.connected(connection)
    else:
        runtime.set_enabled(connection.id, False)
        runtime.set_enabled(connection.id, True)
    entry = gate.registry.entry(connection.id)
    assert entry.availability == "unknown"
    assert entry.error_code is None
    assert not entry.healthy_operations


def test_off_of_previous_interaction_does_not_stop_other_binding_wait():
    from dataclasses import replace
    from tests.unit.test_tool_use import (
        runtime as tool_runtime,
        Decisions,
        InputSource,
        call,
    )
    from app.tool_use.routing import ToolDecision

    async def run():
        decisions = Decisions(call, ToolDecision("clarify", instruction="色は？"))
        async with tool_runtime(decisions, source_type=InputSource) as (
            service,
            source,
            gate,
        ):
            result = await service.run("miori", "session", "操作して")
            assert result.waiting
            run = service._runs[("miori", "session")]
            assert run.candidate is not None
            # 前回のMRTR候補を保持したまま、別接続のbinding待ちに進んだ状態。
            run.interaction = None
            run.binding_candidate = replace(run.candidate, connection_id="other")
            service.connection_disabled(source.connection.id)
            assert service._runs[("miori", "session")] is run
            assert run.loop in gate._loops
            service.connection_disabled("other")
            assert not service._runs and not gate._loops

    asyncio.run(run())


def test_browser_evidence_keeps_only_complete_fixed_events():
    import runpy
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts/acceptance_addon_admin.py"
    sanitize = runpy.run_path(str(script))["sanitized_browser_log"]
    lines = [
        "private endpoint and credential",
        'ADDON_ACCEPTANCE {"check":"restart-restores-off","status":"passed"}',
        'ADDON_ACCEPTANCE {"check":"on-rechecks-health","status":"passed"}',
    ]
    result = sanitize("\n".join(lines), "restored")
    assert "private" not in result
    assert len(result.splitlines()) == 2
    with pytest.raises(RuntimeError, match="incomplete browser evidence"):
        sanitize(lines[1], "restored")
    with pytest.raises(RuntimeError, match="invalid browser evidence"):
        sanitize(lines[1].replace('"passed"', '"passed", "secret":"private"'), "restored")


@pytest.mark.parametrize("enabled", [False, True])
def test_post_replace_failure_matches_runtime_restart_and_gate(tmp_path, monkeypatch, enabled):
    from app.addon_admin.store import SettingsDurabilityError

    async def run():
        connection, gate, runtime = setup(tmp_path, enabled=not enabled)
        source = FakeSource(connection)
        disabled = []
        runtime.on_disabled = disabled.append
        async with gate.attach(connection.id, source):
            loop = gate.begin_loop(CTX)
            generation = gate.registry.entry(connection.id).generation
            with monkeypatch.context() as patch:
                def fail(_):
                    raise OSError("private storage error")
                patch.setattr("app.addon_admin.store.fsync_directory", fail)
                with pytest.raises(SettingsDurabilityError):
                    runtime.set_enabled(connection.id, enabled)
                assert runtime.store.initial(connection.id, not enabled) is enabled
                _, _, restored = setup(tmp_path, enabled=not enabled)
                assert restored.list()[0]["desired_enabled"] is enabled
            assert runtime.list()[0]["desired_enabled"] is enabled
            assert not gate.catalog_snapshots(loop)
            assert disabled == ([] if enabled else [connection.id])
            # 同じ希望値の再保存は耐久化だけを再試行し、旧loopを復活させない。
            runtime.set_enabled(connection.id, enabled)
            assert gate.registry.entry(connection.id).generation == generation + 1
            assert not gate.catalog_snapshots(loop)
            gate.end_loop(loop)

    asyncio.run(run())

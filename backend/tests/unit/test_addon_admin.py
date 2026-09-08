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
        self.closed = False

    @asynccontextmanager
    async def connect(self):
        try:
            yield self
        finally:
            self.closed = True

    async def health(self):
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
        policy = HealthPolicy(interval=0.04, timeout=0.02, disconnect_poll=0.005)
        runtime = AddonRuntime(gate, policy=policy, client_factory=lambda _: client)
        await runtime.start()
        try:
            await eventually(lambda: runtime.list()[0]["availability"] == "available")
            client.health_error = TimeoutError()
            await eventually(lambda: client.health_calls == 1)
            assert runtime.list()[0]["availability"] == "available"
            await eventually(lambda: client.health_calls >= 3)
            assert runtime.list()[0]["availability"] == "unavailable"
            assert runtime.list()[0]["error_code"] == "health_check_failed"
            client.health_error = None
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

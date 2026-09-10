"""管理session・希望状態・設定変更とdispatchの競合を制御したfixtureで検証する。"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from app.addon_admin.connections import ConnectionStore
from app.addon_admin.management import ConnectionManagement
from app.addon_admin.runtime import HealthPolicy
from app.external_mcp import Discovery, ExecutionContext, ExecutionGate, Registry
from app.external_mcp.models import MCPFailure
from app.routers.addon_admin import router
from tests.external_mcp_test_support import FakeSource, discovery
from tests.unit.test_mcp_connection_store import spec, token


class Client(FakeSource):
    def __init__(self, connection, owner):
        super().__init__(connection)
        self.owner = owner
        self.connected = False
        self.connection_failure = None

    @asynccontextmanager
    async def connect(self):
        self.owner.connects += 1
        if self.owner.fail:
            raise MCPFailure("auth", "auth_failed")
        self.connected = True
        try:
            yield self
        finally:
            self.connected = False

    async def discover(self):
        self.owner.discoveries += 1
        if self.owner.pause_check:
            self.owner.check_started.set()
            await self.owner.release_check.wait()
        if self.owner.fail:
            raise MCPFailure("auth", "auth_failed")
        return Discovery("2026-07-28") if self.owner.empty else discovery("native-tool")

    async def health(self):
        if self.owner.fail:
            raise MCPFailure("auth", "auth_failed")

    async def call_tool(self, *args, **kwargs):
        self.owner.tool_started.set()
        await self.owner.release_tool.wait()
        return await super().call_tool(*args, **kwargs)


class Factory:
    fail = False
    empty = False
    pause_check = False
    connects = 0
    discoveries = 0

    def __init__(self):
        self.check_started = asyncio.Event()
        self.release_check = asyncio.Event()
        self.tool_started = asyncio.Event()
        self.release_tool = asyncio.Event()

    def __call__(self, connection):
        return Client(connection, self)


def setup(tmp_path):
    factory = Factory()
    store = ConnectionStore(tmp_path / "mcp" / "connections.sqlite3")
    gate = ExecutionGate(Registry())
    runtime = ConnectionManagement(
        gate,
        store,
        settings_path=tmp_path / "settings.json",
        client_factory=factory,
        policy=HealthPolicy(
            interval=100, connect_timeout=1, timeout=1, disconnect_poll=0.01
        ),
    )
    return runtime, factory


def test_registration_discovery_zero_tools_and_enable_reuse_session(tmp_path):
    async def run():
        runtime, factory = setup(tmp_path)
        factory.empty = True
        try:
            result = await runtime.create(spec(auth="none"))
            cid = result["connection_instance_id"]
            assert result["desired_enabled"] is False
            assert result["last_success_at"]
            assert result["capabilities"]["counts"] == {
                "tools": 0,
                "resources": 0,
                "prompts": 0,
            }
            assert factory.connects == factory.discoveries == 1
            enabled = await runtime.enable(cid, True)
            assert enabled["desired_enabled"] and enabled["availability"] == "available"
            await asyncio.sleep(0.03)
            assert factory.connects == 1
            assert factory.discoveries == 2
        finally:
            await runtime.close()

    asyncio.run(run())


def test_failure_saves_off_and_unconfirmed_enable_stays_off(tmp_path):
    async def run():
        runtime, factory = setup(tmp_path)
        factory.fail = True
        try:
            result = await runtime.create(spec())
            cid = result["connection_instance_id"]
            assert not result["desired_enabled"]
            assert result["last_success_at"] is None
            assert result["last_check_error"] == "authentication_failed"
            with pytest.raises(MCPFailure, match="connection_unconfirmed"):
                await runtime.enable(cid, True)
            assert not runtime.registry.entry(cid).desired_enabled
            factory.fail = False
            assert (await runtime.enable(cid, True))["availability"] == "available"
        finally:
            await runtime.close()

    asyncio.run(run())


def test_on_edit_failure_retains_intent_and_manual_recovery(tmp_path):
    async def run():
        runtime, factory = setup(tmp_path)
        try:
            result = await runtime.create(spec())
            cid = result["connection_instance_id"]
            await runtime.enable(cid, True)
            before = runtime.detail(cid)
            renamed = await runtime.update(cid, spec("名前だけ"))
            assert renamed["last_success_at"] == before["last_success_at"]
            factory.fail = True
            result = await runtime.update(
                cid, spec(endpoint="http://127.0.0.1:9101/mcp")
            )
            assert result["desired_enabled"]
            assert result["availability"] == "unavailable"
            assert result["last_success_at"] is None
            factory.fail = False
            result = await runtime.check(cid)
            assert result["desired_enabled"] and result["availability"] == "available"
            success = result["last_success_at"]
            factory.fail = True
            result = await runtime.check(cid)
            assert result["last_success_at"] == success
            assert result["last_check_error"] == "authentication_failed"
        finally:
            await runtime.close()

    asyncio.run(run())


def test_active_tool_blocks_setting_credential_delete_but_not_display_name(tmp_path):
    async def run():
        runtime, factory = setup(tmp_path)
        try:
            result = await runtime.create(spec())
            cid = result["connection_instance_id"]
            await runtime.enable(cid, True)
            loop = runtime.gate.begin_loop(ExecutionContext("miori", "management-test"))
            execution = asyncio.create_task(
                runtime.gate.invoke(cid, "native-tool", {"value": 1}, loop)
            )
            await asyncio.wait_for(factory.tool_started.wait(), 1)
            for call in (
                lambda: runtime.update(cid, spec(endpoint="http://127.0.0.1:9102/mcp")),
                lambda: runtime.credential(cid, token()),
                lambda: runtime.delete(cid),
            ):
                with pytest.raises(MCPFailure, match="connection_busy"):
                    await call()
            renamed = await runtime.update(cid, spec("実行中でも表示名変更"))
            assert renamed["last_success_at"]
            factory.release_tool.set()
            assert (await execution)["outcome"] == "succeeded"
            await runtime.delete(cid)
            assert runtime.list() == []
        finally:
            factory.release_tool.set()
            await runtime.close()

    asyncio.run(run())


def test_off_cancels_inflight_enable_and_old_check_cannot_restore_it(tmp_path):
    async def run():
        runtime, factory = setup(tmp_path)
        try:
            cid = (await runtime.create(spec()))["connection_instance_id"]
            factory.pause_check = True
            enable = asyncio.create_task(runtime.enable(cid, True))
            await asyncio.wait_for(factory.check_started.wait(), 1)
            await runtime.enable(cid, False)
            factory.release_check.set()
            with pytest.raises(MCPFailure, match="connection_changed"):
                await enable
            assert not runtime.registry.entry(cid).desired_enabled
        finally:
            factory.release_check.set()
            await runtime.close()

    asyncio.run(run())


def test_edit_cancels_stale_check_and_does_not_record_its_success(tmp_path):
    async def run():
        runtime, factory = setup(tmp_path)
        try:
            cid = (await runtime.create(spec()))["connection_instance_id"]
            factory.pause_check = True
            check = asyncio.create_task(runtime.check(cid))
            await asyncio.wait_for(factory.check_started.wait(), 1)
            factory.pause_check = False
            updated = await runtime.update(
                cid, spec(endpoint="http://127.0.0.1:9104/mcp")
            )
            with pytest.raises(MCPFailure, match="connection_changed"):
                await check
            assert updated["settings_revision"] == 2
            assert updated["last_success_at"]
            factory.release_check.set()
            await asyncio.sleep(0.02)
            assert runtime.detail(cid)["settings_revision"] == 2
        finally:
            factory.release_check.set()
            await runtime.close()

    asyncio.run(run())


def test_management_api_rejects_raw_fields_and_credentials_never_echo(tmp_path):
    async def run():
        runtime, factory = setup(tmp_path)
        app = FastAPI()
        app.state.addon_manager = runtime
        app.include_router(router)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                base = "/addon-admin/external-connections"
                result = await client.post(base, json=spec().model_dump())
                assert result.status_code == 201
                cid = result.json()["connection_instance_id"]
                assert result.headers["cache-control"] == "no-store"
                for method, path, payload in (
                    (
                        "POST",
                        base,
                        {**spec().model_dump(), "token": "synthetic-private-token"},
                    ),
                    (
                        "PUT",
                        f"{base}/{cid}/credential",
                        {"token": "synthetic-private-token", "secret_ref": "OTHER"},
                    ),
                    (
                        "PUT",
                        f"{base}/{cid}/credential",
                        {"token": "synthetic-private-token\n"},
                    ),
                ):
                    response = await client.request(method, path, json=payload)
                    assert response.status_code == 422
                    assert "synthetic-private-token" not in response.text
                response = await client.put(
                    f"{base}/{cid}/credential",
                    json={"token": "synthetic-private-token"},
                )
                assert response.status_code == 200
                assert response.json()["credential_set"]
                assert "synthetic-private-token" not in response.text
                assert "secret_ref" not in response.json()
                assert (await client.delete(f"{base}/{cid}")).status_code == 204
                assert (await client.get(f"{base}/{cid}")).status_code == 404
        finally:
            await runtime.close()

    asyncio.run(run())


def test_managed_runtime_preserves_self_owned_registration_and_switch(tmp_path):
    from app.external_mcp.models import Connection, encode
    from app.tool_use.runtime import ToolRuntime, ToolSettings
    from tests.external_mcp_test_support import manifest

    async def run():
        value = manifest(connection_id="self-owned")
        value["connection"]["ownership"] = "self_owned"
        runtime = ToolRuntime(
            ToolSettings(connections=(Connection(encode(value)),)),
            None,
            None,
            settings_path=tmp_path / "settings.json",
        )
        manager = runtime.management
        assert isinstance(manager, ConnectionManagement)
        try:
            assert manager.connections.records() == ()
            assert manager.list()[0]["source_type"] == "self_owned"
            assert not (await manager.enable("self-owned", False))["desired_enabled"]
            with pytest.raises(MCPFailure, match="external_connection_required"):
                manager.detail("self-owned")
        finally:
            await runtime.close()

    asyncio.run(run())


def test_delete_cancels_late_discovery_and_cannot_restore_connection(tmp_path):
    async def run():
        runtime, factory = setup(tmp_path)
        try:
            cid = (await runtime.create(spec()))["connection_instance_id"]
            factory.pause_check = True
            check = asyncio.create_task(runtime.check(cid))
            await asyncio.wait_for(factory.check_started.wait(), 1)
            await runtime.delete(cid)
            factory.release_check.set()
            with pytest.raises(MCPFailure, match="connection_changed"):
                await check
            await asyncio.sleep(.02)
            assert runtime.list() == []
            assert runtime.connections.records() == ()
        finally:
            factory.release_check.set()
            await runtime.close()
    asyncio.run(run())


def test_changed_settings_clear_old_attempt_while_new_check_is_pending(tmp_path):
    async def run():
        runtime, factory = setup(tmp_path)
        try:
            cid = (await runtime.create(spec()))["connection_instance_id"]
            assert runtime.detail(cid)["last_checked_at"]
            factory.pause_check = True
            update = asyncio.create_task(runtime.update(
                cid, spec(endpoint="http://127.0.0.1:9105/mcp")
            ))
            await asyncio.wait_for(factory.check_started.wait(), 1)
            pending = runtime.detail(cid)
            assert pending["availability"] == "unknown"
            assert pending["last_success_at"] is None
            assert pending["last_checked_at"] is None
            factory.release_check.set()
            assert (await update)["last_checked_at"]
        finally:
            factory.release_check.set()
            await runtime.close()
    asyncio.run(run())


def test_dynamic_credential_is_redacted_before_output_truncation():
    from app.tool_use.projection import Sanitizer
    from tests.unit.test_tool_use import Scanner

    secret = "synthetic-private-credential"
    sanitizer = Sanitizer(Scanner(), dynamic_private_values=lambda: (secret,))
    result = sanitizer.text("12345678" + secret, maximum=12)
    assert "synt" not in result
    assert result == "12345678[非公開"

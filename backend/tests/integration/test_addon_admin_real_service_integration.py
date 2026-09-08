"""公開Filesystem/Everythingへの実接続で管理APIと無操作時の障害検知を確認する。"""

import asyncio
import os

import httpx
import pytest
from fastapi import FastAPI

from app.addon_admin.runtime import AddonRuntime, HealthPolicy
from app.external_mcp import Connection, ExecutionContext, ExecutionGate, Registry
from app.routers.addon_admin import router
from tests.external_mcp_test_support import manifest
from tests.integration.test_external_mcp_real_servers_integration import (
    everything_http,
    servers,  # noqa: F401
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MCP_REAL_SERVICE_TESTS") != "true",
    reason="公開MCPの実接続はRUN_MCP_REAL_SERVICE_TESTS=trueで明示実行する",
)


async def wait_state(runtime, state):
    async with asyncio.timeout(20):
        while runtime.list()[0]["availability"] != state:
            await asyncio.sleep(0.05)


def test_published_mcp_management_and_idle_disconnect(servers, tmp_path):
    async def run(endpoint, process):
        c = Connection.from_manifest(
            manifest(transport="streamable_http", endpoint=endpoint)
        )
        registry = Registry()
        registry.register(c, desired_enabled=False)
        gate = ExecutionGate(registry)
        runtime = AddonRuntime(
            gate,
            settings_path=tmp_path / "settings.json",
            policy=HealthPolicy(interval=0.2, timeout=0.5, disconnect_poll=0.05),
        )
        app = FastAPI()
        app.include_router(router)
        app.state.addon_manager = runtime
        await runtime.start()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as api:
                rows = (await api.get("/addon-admin/connections")).json()
                assert rows[0]["effective_state"] == "disabled"
                assert rows[0]["availability"] == "unknown"
                response = await api.patch(
                    f"/addon-admin/connections/{c.id}", json={"desired_enabled": True}
                )
                assert response.status_code == 200
                await wait_state(runtime, "available")
                loop = gate.begin_loop(ExecutionContext("integration", "admin"))
                assert (
                    await gate.invoke(
                        c.id, "echo", {"message": "admin-real-acceptance"}, loop
                    )
                )["outcome"] == "succeeded"
                await api.patch(
                    f"/addon-admin/connections/{c.id}", json={"desired_enabled": False}
                )
                assert not gate.catalog_snapshots(loop)
                assert (await gate.invoke(c.id, "echo", {"message": "disabled"}, loop))[
                    "outcome"
                ] == "failed"
                assert process.poll() is None
                await api.patch(
                    f"/addon-admin/connections/{c.id}", json={"desired_enabled": True}
                )
                await wait_state(runtime, "available")
                assert (await gate.invoke(c.id, "echo", {"message": "old-loop"}, loop))[
                    "outcome"
                ] == "failed"
                gate.end_loop(loop)
                checked = runtime.list()[0]["last_checked_at"]
                await asyncio.sleep(0.5)
                assert runtime.list()[0]["last_checked_at"] != checked
                # Toolを実行せず公開HTTP serverを停止し、healthだけで収束させる。
                process.terminate()
                process.wait(timeout=5)
                await wait_state(runtime, "unavailable")
                row = (await api.get("/addon-admin/connections")).json()[0]
                assert row["desired_enabled"] is True
                assert endpoint not in str(row)
                assert row["error_code"] in {"connection_failed", "health_check_failed"}
                assert (
                    await api.patch(
                        f"/addon-admin/connections/{c.id}",
                        json={"desired_enabled": False},
                    )
                ).status_code == 200
        finally:
            await runtime.close()
        assert not runtime._tasks
        print(
            "published Everything: API ON/OFF, real Tool, stale-loop rejection, ping, idle disconnect, teardown=passed"
        )

    with everything_http(servers, tmp_path) as (endpoint, process):
        asyncio.run(run(endpoint, process))

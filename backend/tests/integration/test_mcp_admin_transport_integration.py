"""実MCPの受信済みTool実行中に管理変更を拒否する。第三者サービス受入とは区別する。"""

import asyncio
import os

import pytest

from app.addon_admin.connections import ConnectionStore
from app.addon_admin.management import ConnectionManagement
from app.addon_admin.runtime import HealthPolicy
from app.external_mcp import ExecutionContext, ExecutionGate, Registry
from app.external_mcp.models import MCPFailure
from tests.module.test_external_mcp_conformance import http_server
from tests.unit.test_mcp_connection_store import spec, token

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MCP_ADMIN_REAL_TESTS") != "true",
    reason="RUN_MCP_ADMIN_REAL_TESTS=trueで実HTTP/別processを起動する",
)


def test_received_tool_blocks_mutations_and_recovery_invalidates_old_loop(
    tmp_path, caplog
):
    marker = tmp_path / "active"
    with http_server("bearer", active_marker=marker) as endpoint:

        async def run():
            store = ConnectionStore(tmp_path / "management" / "connections.sqlite3")
            gate = ExecutionGate(Registry())
            runtime = ConnectionManagement(
                gate,
                store,
                settings_path=tmp_path / "settings.json",
                policy=HealthPolicy(interval=100, connect_timeout=5, timeout=2),
            )
            execution = None
            try:
                registered = await runtime.create(spec(endpoint=endpoint))
                cid = registered["connection_instance_id"]
                assert registered["last_success_at"] is None
                await runtime.credential(cid, token("synthetic-test-token"))
                await runtime.enable(cid, True)
                loop = gate.begin_loop(ExecutionContext("miori", "mcp-admin-real"))
                execution = asyncio.create_task(
                    gate.invoke(cid, "timeout", {"value": 1}, loop)
                )
                async with asyncio.timeout(5):
                    while not marker.exists():
                        await asyncio.sleep(0.01)
                # 別processが実際にTool requestを受信した印を確認してから操作する。
                assert marker.read_text() == "started"
                for operation in (
                    lambda: runtime.update(cid, spec(endpoint=endpoint, auth="none")),
                    lambda: runtime.credential(cid, token("replacement-token")),
                    lambda: runtime.delete(cid),
                ):
                    with pytest.raises(MCPFailure, match="connection_busy"):
                        await operation()
                renamed = await runtime.update(
                    cid, spec("実行中の表示名", endpoint=endpoint)
                )
                assert renamed["last_success_at"]
                marker.unlink()
                assert (await execution)["outcome"] == "succeeded"
                changed = await runtime.update(
                    cid, spec(endpoint=endpoint, auth="none")
                )
                assert changed["desired_enabled"]
                assert changed["last_success_at"] is None
                assert changed["availability"] == "unavailable"
                await runtime.update(cid, spec(endpoint=endpoint))
                recovered = await runtime.credential(cid, token("synthetic-test-token"))
                assert recovered["availability"] == "available"
                assert recovered["desired_enabled"]
                old = await gate.invoke(cid, "native", {"value": 1}, loop)
                assert old["outcome"] != "succeeded"
                new_loop = gate.begin_loop(
                    ExecutionContext("miori", "mcp-admin-recovered")
                )
                assert (await gate.invoke(cid, "native", {"value": 1}, new_loop))[
                    "outcome"
                ] == "succeeded"
                await runtime.delete(cid)
                assert runtime.list() == []
                print(
                    "actual received Tool: mutation rejection / name update / recovery / old-loop rejection passed"
                )
            finally:
                marker.unlink(missing_ok=True)
                if execution is not None and not execution.done():
                    await execution
                await runtime.close()

        asyncio.run(run())
    for private in (
        "synthetic-test-token",
        "replacement-token",
        "private-auth-error",
        "native-private-payload",
    ):
        assert private not in caplog.text

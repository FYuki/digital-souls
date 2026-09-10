"""#154の単体検証。"""


def test_metadata_or_protocol_failure_is_not_retryable():
    from app.external_mcp.client import _failure
    from mcp.shared.exceptions import MCPError

    assert not _failure(ValueError("private-result")).retryable
    assert not _failure(MCPError(-32602, "private-protocol")).retryable
    assert _failure(TimeoutError()).retryable
    assert "private" not in str(_failure(ValueError("private-result")))


def test_health_uses_protocol_supported_read_only_probe():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.external_mcp.client import ExternalMCPClient
    from app.external_mcp import Connection
    from tests.external_mcp_test_support import manifest

    async def run():
        adapter = ExternalMCPClient(Connection.from_manifest(manifest()))
        # 実transportの新旧probeはmodule conformanceでも確認する。
        session = SimpleNamespace(send_discover=AsyncMock(return_value={
            "supportedVersions": ["2026-07-28"], "capabilities": {},
            "resultType": "complete", "ttlMs": 0, "cacheScope": "private",
        }), send_ping=AsyncMock())
        client = SimpleNamespace(protocol_version="2026-07-28", session=session)
        async def request(operation):
            return await operation(client)
        adapter._request = request
        await adapter.health()
        session.send_discover.assert_awaited_once_with("2026-07-28")
        session.send_ping.assert_not_awaited()
        client.protocol_version = "2025-11-25"
        await adapter.health()
        session.send_ping.assert_awaited_once()
    asyncio.run(run())

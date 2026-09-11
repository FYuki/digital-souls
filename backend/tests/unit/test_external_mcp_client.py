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
        session = SimpleNamespace(
            send_discover=AsyncMock(
                return_value={
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {},
                    "resultType": "complete",
                    "ttlMs": 0,
                    "cacheScope": "private",
                }
            ),
            send_ping=AsyncMock(),
        )
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


def test_request_marks_credential_preflight_and_transport_failure_separately():
    import asyncio
    import pytest
    from app.external_mcp.client import ExternalMCPClient
    from app.external_mcp import Connection
    from app.external_mcp.models import MCPFailure
    from tests.external_mcp_test_support import manifest

    async def run():
        class Secrets:
            allowed = False

            async def resolve(self, ref):
                if not self.allowed:
                    raise ValueError("private-secret")
                return "fixture-token"

        secrets = Secrets()
        adapter = ExternalMCPClient(
            Connection.from_manifest(
                manifest(
                    transport="streamable_http",
                    auth={"type": "bearer", "secret_ref": "FIXTURE_SECRET"},
                )
            ),
            secrets=secrets,
        )
        adapter._client = object()
        adapter._owner = asyncio.create_task(asyncio.sleep(60))
        dispatched = []

        async def operation(client):
            dispatched.append(True)
            raise TimeoutError("private-result")

        try:
            with pytest.raises(MCPFailure) as failed:
                await adapter._request(operation)
            assert failed.value.request_started is False and not dispatched
            secrets.allowed = True
            with pytest.raises(MCPFailure) as uncertain:
                await adapter._request(operation)
            assert uncertain.value.request_started is True and dispatched
            assert "private" not in str(uncertain.value)
        finally:
            adapter._owner.cancel()
            await asyncio.gather(adapter._owner, return_exceptions=True)

    asyncio.run(run())


def test_connection_failure_preserves_retryability_without_sending():
    import asyncio
    import pytest
    from app.external_mcp import Connection, ExternalMCPClient
    from app.external_mcp.models import MCPFailure
    from tests.external_mcp_test_support import manifest

    async def run():
        client = ExternalMCPClient(Connection.from_manifest(manifest()))
        client._connection_failure = MCPFailure("transport", "transport_timeout", retryable=True)
        async def never(_):
            raise AssertionError("dispatch must not start")
        with pytest.raises(MCPFailure) as failed:
            await client._request(never)
        assert failed.value.retryable and failed.value.request_started is False
        assert failed.value.category == "transport" and failed.value.code == "transport_timeout"
    asyncio.run(run())

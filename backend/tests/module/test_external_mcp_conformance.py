"""SDK pinと別processのexternal stdio/HTTP公開protocol適合。"""

import asyncio
import importlib.metadata
import json
import logging
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.external_mcp import (
    Connection,
    ExecutionContext,
    ExecutionGate,
    ExternalMCPClient,
    MCPFailure,
    Registry,
)
from tests.external_mcp_test_support import manifest

SERVER = Path(__file__).resolve().parents[1] / "fixtures/external_mcp/server.py"
CTX = ExecutionContext("miori", "conformance")


def stdio_connection(tmp_path, *, legacy=False):
    value = manifest()
    value["connection"]["stdio"] = {
        "command": sys.executable,
        "args": [
            str(SERVER),
            "--state",
            str(tmp_path / "state.json"),
            "--pid",
            str(tmp_path / "pid"),
        ]
        + (["--legacy"] if legacy else []),
    }
    (tmp_path / "state.json").write_text('{"revision":1}')
    return Connection.from_manifest(value)


@contextmanager
def http_server(auth="none", fault_state=None, *, with_process=False):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(port), "--auth", auth]
        + (["--fault-state", str(fault_state)] if fault_state else []),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError(process.stderr.read().decode())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("test MCP startup timeout")
        endpoint = f"http://127.0.0.1:{port}/mcp"
        yield (endpoint, process) if with_process else endpoint
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()


async def exercise(connection, *, expect_mrtr=True):
    registry = Registry()
    registry.register(connection)
    gate = ExecutionGate(registry)
    adapter = ExternalMCPClient(connection, timeout=3)
    async with adapter.connect(), gate.attach(connection.id, adapter):
        loop = gate.begin_loop(CTX)
        active = registry.entry(connection.id).active.document
        assert len(active["tools"]) == 6
        assert active["protocol_version"] in {"2026-07-28", "2025-11-25"}
        normal = await gate.invoke(connection.id, "native", {"value": 1}, loop)
        assert normal["outcome"] == "succeeded", normal
        if expect_mrtr:
            content = normal["native_payload"]["content"]
            assert [c["type"] for c in content] == [
                "text",
                "image",
                "resource_link",
                "resource",
            ]
            assert normal["native_payload"]["structuredContent"] == {
                "arguments": {"value": 1}
            }
            assert active["prompts"][0]["name"] == "untrusted"
            assert "IGNORE-THIS" not in json.dumps(active)
            resource = await gate.read_resource(connection.id, "test://resource", loop)
            assert resource["outcome"] == "succeeded", resource
            assert (
                resource["native_payload"]["contents"][0]["text"] == "native-resource"
            )
            interaction = await gate.invoke(connection.id, "input", {"value": 1}, loop)
            assert interaction["outcome"] == "input_required", interaction
            final = await gate.resume(
                interaction["interaction_id"],
                {"question": {"action": "accept", "content": {"ok": True}}},
                loop,
            )
            assert final["outcome"] == "succeeded", final
            # SDKが捨てる旧executionを_metaの独自契約へ置換しない。
            task_definition = next(t for t in active["tools"] if t["name"] == "task")
            assert task_definition["status"] == "active"
            task = await gate.invoke(connection.id, "task", {"value": 1}, loop)
            assert task["outcome"] == "unsupported", task
            error = await gate.invoke(connection.id, "error", {"value": 1}, loop)
            assert error["error_category"] == "tool_error"
    assert not adapter.connected
    assert registry.entry(connection.id).availability == "unavailable"


def test_stdio_native_mrtr_and_cleanup(tmp_path, caplog):
    assert importlib.metadata.version("mcp") == "2.0.0"
    caplog.set_level(logging.DEBUG)
    connection = stdio_connection(tmp_path)
    asyncio.run(exercise(connection))
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert "native-private-payload" not in caplog.text
    assert "private-tool-error" not in caplog.text


def test_legacy_negotiation_fallback(tmp_path):
    asyncio.run(exercise(stdio_connection(tmp_path, legacy=True), expect_mrtr=False))


@pytest.mark.parametrize("auth", ["none", "bearer"])
def test_http_native_mrtr(auth, monkeypatch, caplog):
    monkeypatch.setenv("MCP_TEST_TOKEN", "synthetic-test-token")
    caplog.set_level(logging.DEBUG)
    with http_server(auth) as endpoint:
        connection = Connection.from_manifest(
            manifest(
                transport="streamable_http",
                endpoint=endpoint,
                auth={"type": "bearer", "secret_ref": "MCP_TEST_TOKEN"}
                if auth == "bearer"
                else None,
            )
        )
        asyncio.run(exercise(connection))
    assert "synthetic-test-token" not in caplog.text
    assert "native-private-payload" not in caplog.text


@pytest.mark.parametrize(
    "auth,code", [("bearer", "auth_failed"), ("oauth", "unsupported_auth")]
)
def test_auth_failure_is_sanitized(auth, code, caplog):
    caplog.set_level(logging.DEBUG)

    async def run(connection):
        with pytest.raises(MCPFailure) as caught:
            async with ExternalMCPClient(connection, timeout=2).connect():
                pytest.fail("authentication must fail")
        assert caught.value.category == "auth"
        assert caught.value.code == code
        assert "private-auth-error" not in str(caught.value)

    with http_server(auth) as endpoint:
        c = Connection.from_manifest(
            manifest(transport="streamable_http", endpoint=endpoint)
        )
        asyncio.run(run(c))
    assert "private-auth-error" not in caplog.text


def test_refresh_and_input_schema_change(tmp_path):
    async def run():
        c = stdio_connection(tmp_path)
        r = Registry()
        r.register(c)
        client = ExternalMCPClient(c)
        gate = ExecutionGate(r)
        async with client.connect(), gate.attach(c.id, client):
            loop = gate.begin_loop(CTX)
            previous = r.entry(c.id).active
            (tmp_path / "state.json").write_text('{"revision":2}')
            staged = await gate.refresh(c.id)
            assert staged.revision != previous.revision
            assert r.entry(c.id).active == previous
            assert (await gate.invoke(c.id, "new", {"value": "a"}, loop))[
                "outcome"
            ] == "failed"
            loop2 = gate.begin_loop(CTX)
            assert (await gate.invoke(c.id, "old", {"value": 1}, loop2))[
                "outcome"
            ] == "failed"
            assert (await gate.invoke(c.id, "native", {"value": 1}, loop2))[
                "error_category"
            ] == "validation"
            assert (await gate.invoke(c.id, "native", {"value": "a"}, loop2))[
                "outcome"
            ] == "succeeded"

    asyncio.run(run())


def test_timeout_closes_stdio_process(tmp_path):
    async def run():
        c = stdio_connection(tmp_path)
        r = Registry()
        r.register(c)
        adapter = ExternalMCPClient(c, timeout=0.3)
        gate = ExecutionGate(r)
        async with adapter.connect(), gate.attach(c.id, adapter):
            result = await gate.invoke(
                c.id, "timeout", {"value": 1}, gate.begin_loop(CTX)
            )
            assert result["error_category"] == "transport"
            assert result["retry_count"] == 0

    asyncio.run(run())
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "pid").read_text()), 0)


@pytest.mark.parametrize("trusted,attempts", [(False, 1), (True, 2)])
def test_http_retry_requires_annotation_trust(tmp_path, trusted, attempts, caplog):
    caplog.set_level(logging.DEBUG)

    async def run(endpoint):
        c = Connection.from_manifest(
            manifest(transport="streamable_http", endpoint=endpoint, trusted=trusted)
        )
        registry = Registry()
        registry.register(c)
        gate = ExecutionGate(registry)
        client = ExternalMCPClient(c)
        async with client.connect(), gate.attach(c.id, client):
            result = await gate.invoke(
                c.id, "native", {"value": 1}, gate.begin_loop(CTX)
            )
            assert result["retry_count"] == attempts - 1, result
            assert result["outcome"] == ("succeeded" if trusted else "failed"), result

    fault = tmp_path / "fault-count"
    with http_server(fault_state=fault) as endpoint:
        asyncio.run(run(endpoint))
    assert int(fault.read_text()) == attempts
    assert "private-transient-error" not in caplog.text


def test_bearer_is_resolved_per_request_and_invalid_rotation_is_contained(
    monkeypatch, caplog
):
    async def run(endpoint):
        c = Connection.from_manifest(
            manifest(
                transport="streamable_http",
                endpoint=endpoint,
                auth={"type": "bearer", "secret_ref": "MCP_TEST_TOKEN"},
            )
        )
        r = Registry()
        r.register(c)
        gate = ExecutionGate(r)
        client = ExternalMCPClient(c)
        monkeypatch.setenv("MCP_TEST_TOKEN", "synthetic-test-token")
        async with client.connect(), gate.attach(c.id, client):
            loop = gate.begin_loop(CTX)
            monkeypatch.delenv("MCP_TEST_TOKEN")
            missing = await gate.invoke(c.id, "native", {"value": 0}, loop)
            assert missing["error_category"] == "auth", missing
            monkeypatch.setenv("MCP_TEST_TOKEN", "private-invalid-token")
            result = await gate.invoke(c.id, "native", {"value": 1}, loop)
            assert result["error_category"] == "auth", result
            assert result["retry_count"] == 0
            monkeypatch.setenv("MCP_TEST_TOKEN", "synthetic-test-token")
            result = await gate.invoke(c.id, "native", {"value": 2}, loop)
            assert result["outcome"] == "succeeded", result

    caplog.set_level(logging.DEBUG)
    with http_server("bearer") as endpoint:
        asyncio.run(run(endpoint))
    assert "private-invalid-token" not in caplog.text
    assert "private-auth-error" not in caplog.text


def test_http_disconnect_does_not_cancel_caller():
    async def run(endpoint, process):
        c = Connection.from_manifest(
            manifest(transport="streamable_http", endpoint=endpoint)
        )
        registry = Registry()
        registry.register(c)
        gate = ExecutionGate(registry)
        client = ExternalMCPClient(c, timeout=2)
        async with client.connect(), gate.attach(c.id, client):
            loop = gate.begin_loop(CTX)
            process.terminate()
            process.wait(timeout=5)
            result = await gate.invoke(c.id, "native", {"value": 1}, loop)
            assert result["outcome"] == "failed"
            assert result["error_category"] == "transport"
            assert result["retry_count"] == 0
            # SDK内部のcancel scopeがCoreのtaskを取り消していない。
            await asyncio.sleep(0)
            assert asyncio.current_task().cancelling() == 0
        assert not client.connected

    with http_server(with_process=True) as (endpoint, process):
        asyncio.run(run(endpoint, process))


def test_caller_cancellation_still_propagates(tmp_path):
    async def run():
        entered = asyncio.Event()
        client = ExternalMCPClient(stdio_connection(tmp_path), timeout=2)

        async def use_connection():
            async with client.connect():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(use_connection())
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not client.connected

    asyncio.run(run())
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "pid").read_text()), 0)

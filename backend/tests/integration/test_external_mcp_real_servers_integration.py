"""公開済みの独立したMCP実装へ実接続する。合成serverへの置換は禁止。"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.external_mcp import (
    Connection,
    ExecutionContext,
    ExecutionGate,
    ExternalMCPClient,
    Registry,
)
from tests.external_mcp_test_support import manifest

VERSION = "2026.8.31"
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MCP_REAL_SERVICE_TESTS") != "true",
    reason="RUN_MCP_REAL_SERVICE_TESTS=true と公開MCP packageの準備が必要",
)


@pytest.fixture
def servers():
    # 明示開始後の準備失敗はskipへ変更しない。
    root = Path(os.environ["MCP_REAL_SERVER_ROOT"]).resolve()
    node = shutil.which("node")
    assert node, "Node.jsが必要"
    result = {"node": node}
    for name in ("filesystem", "everything"):
        package = root / "node_modules" / "@modelcontextprotocol" / f"server-{name}"
        assert json.loads((package / "package.json").read_text())["version"] == VERSION
        result[name] = str(package / "dist/index.js")
    return result


def test_published_filesystem_stdio(servers, tmp_path, caplog):
    data = tmp_path / "allowed"
    data.mkdir()
    sample = data / "sample.txt"
    sample.write_text("mcp-integration-read-evidence", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-must-not-be-returned", encoding="utf-8")
    value = manifest(connection_id="real-filesystem")
    value["connection"]["stdio"] = {
        "command": servers["node"],
        "args": [servers["filesystem"], str(data)],
    }

    async def run():
        c = Connection.from_manifest(value)
        registry = Registry()
        registry.register(c)
        gate = ExecutionGate(registry)
        client = ExternalMCPClient(c, timeout=15)
        async with client.connect(), gate.attach(c.id, client):
            loop = gate.begin_loop(ExecutionContext("integration", "filesystem"))
            snapshot = registry.entry(c.id).active.document
            assert any(t["name"] == "read_text_file" for t in snapshot["tools"])
            result = await gate.invoke(
                c.id, "read_text_file", {"path": str(sample)}, loop
            )
            assert result["outcome"] == "succeeded", result.get("error_category")
            assert any(
                v.get("text") == sample.read_text()
                for v in result["native_payload"]["content"]
            )
            denied = await gate.invoke(
                c.id, "read_text_file", {"path": str(outside)}, loop
            )
            assert denied["error_category"] == "tool_error"
            assert "outside-must-not-be-returned" not in json.dumps(denied)
            print(
                f"filesystem: protocol={snapshot['protocol_version']}; tools={len(snapshot['tools'])}; read/access-denial=passed"
            )
            gate.end_loop(loop)
        assert not client.connected
        assert registry.entry(c.id).availability == "unavailable"

    asyncio.run(run())
    assert "mcp-integration-read-evidence" not in caplog.text


@contextmanager
def everything_http(servers, tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # 個人のsecretや設定をserverへ継承しない。
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "PORT": str(port)}
    process = subprocess.Popen(
        [servers["node"], servers["everything"], "streamableHttp"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(200):
            assert process.poll() is None, "公開MCP serverが起動中に終了"
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("公開MCP serverのreadiness timeout")
        yield f"http://127.0.0.1:{port}/mcp", process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_published_everything_streamable_http(servers, tmp_path, caplog):
    async def run(endpoint, process):
        c = Connection.from_manifest(
            manifest(
                connection_id="real-everything",
                transport="streamable_http",
                endpoint=endpoint,
            )
        )
        registry = Registry()
        registry.register(c)
        gate = ExecutionGate(registry)
        client = ExternalMCPClient(c, timeout=15)
        async with client.connect(), gate.attach(c.id, client):
            loop = gate.begin_loop(ExecutionContext("integration", "everything"))
            snapshot = registry.entry(c.id).active.document
            assert snapshot["prompts"] and snapshot["resources"]
            result = await gate.invoke(
                c.id, "echo", {"message": "mcp-real-http-evidence"}, loop
            )
            assert result["outcome"] == "succeeded", result.get("error_category")
            assert (
                result["native_payload"]["content"][0]["text"]
                == "Echo: mcp-real-http-evidence"
            )
            resource = await gate.read_resource(
                c.id, snapshot["resources"][0]["uri"], loop
            )
            assert resource["outcome"] == "succeeded", resource.get("error_category")
            assert resource["native_payload"]["contents"]
            print(
                f"everything: protocol={snapshot['protocol_version']}; tools={len(snapshot['tools'])}; resources={len(snapshot['resources'])}; prompts={len(snapshot['prompts'])}; echo/resource-read=passed"
            )
            process.terminate()
            process.wait(timeout=5)
            disconnected = await gate.invoke(
                c.id, "echo", {"message": "after-stop"}, loop
            )
            assert disconnected["outcome"] == "failed"
            assert disconnected["error_category"] == "transport"
            assert disconnected["retry_count"] == 0
            gate.end_loop(loop)
        assert not client.connected
        assert registry.entry(c.id).availability == "unavailable"

    with everything_http(servers, tmp_path) as (endpoint, process):
        asyncio.run(run(endpoint, process))
    assert "mcp-real-http-evidence" not in caplog.text


NGINX_IMAGE = (
    "nginx@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284"
)


@contextmanager
def bearer_proxy(upstream, tmp_path, token):
    # 実nginxを認証境界にする。MCP応答をmock/合成しない。
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = tmp_path / "nginx.conf"
    config.write_text(f"""events {{}}
http {{
    map_hash_bucket_size 128;
    access_log off;
    error_log /dev/null crit;
    map $http_authorization $allowed {{ default 0; "Bearer {token}" 1; }}
    server {{
        listen 127.0.0.1:{port};
        location /mcp {{
            if ($allowed = 0) {{ return 401; }}
            proxy_pass {upstream};
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_buffering off;
        }}
    }}
}}
""")
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "-d",
            "--network",
            "host",
            "--entrypoint",
            "nginx",
            "-v",
            f"{config}:/etc/nginx/nginx.conf:ro",
            NGINX_IMAGE,
            "-g",
            "daemon off;",
        ],
        text=True,
    ).strip()
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            detail = subprocess.check_output(
                ["docker", "logs", container], stderr=subprocess.STDOUT, text=True
            )
            pytest.fail(
                "nginx readiness timeout: " + detail.replace(token, "[redacted]")
            )
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL
        )
        config.unlink()


@pytest.mark.parametrize("credential", ["valid", "invalid", "missing"])
def test_real_http_bearer_proxy(servers, tmp_path, monkeypatch, caplog, credential):
    import secrets
    from app.external_mcp import MCPFailure

    token = secrets.token_hex(24)
    monkeypatch.setenv(
        "MCP_REAL_TEST_TOKEN", token if credential == "valid" else "invalid-test-token"
    )

    async def run(endpoint):
        c = Connection.from_manifest(
            manifest(
                connection_id="real-bearer",
                transport="streamable_http",
                endpoint=endpoint,
                auth={"type": "bearer", "secret_ref": "MCP_REAL_TEST_TOKEN"}
                if credential != "missing"
                else None,
            )
        )
        client = ExternalMCPClient(c, timeout=10)
        if credential != "valid":
            with pytest.raises(MCPFailure) as caught:
                async with client.connect():
                    pytest.fail("認証なし/誤tokenで接続成立してはならない")
            assert caught.value.category == "auth"
            assert caught.value.code == "auth_failed"
            return
        registry = Registry()
        registry.register(c)
        gate = ExecutionGate(registry)
        async with client.connect(), gate.attach(c.id, client):
            loop = gate.begin_loop(ExecutionContext("integration", "bearer"))
            result = await gate.invoke(
                c.id, "echo", {"message": "authenticated-read"}, loop
            )
            assert result["outcome"] == "succeeded", result.get("error_category")
            assert (
                result["native_payload"]["content"][0]["text"]
                == "Echo: authenticated-read"
            )
            gate.end_loop(loop)

    with everything_http(servers, tmp_path) as (upstream, _):
        with bearer_proxy(upstream, tmp_path, token) as endpoint:
            asyncio.run(run(endpoint))
    assert token not in caplog.text
    print(f"nginx/everything: bearer-{credential}=passed")

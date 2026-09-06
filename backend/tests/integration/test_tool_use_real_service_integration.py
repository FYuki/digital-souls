"""公開MCPと実LLMによる選択・実行の受入。ブラウザ音声受入は別suite。"""

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from app.external_mcp import Connection, ExecutionGate, ExternalMCPClient, Registry
from app.inference.runtime import create_inference_runtime
from app.memory.memory_policy import resolved_memory_policy
from app.privacy.scanner import create_privacy_scanner
from app.tool_use.binding import BindingResolver
from app.tool_use.projection import Sanitizer
from app.tool_use.routing import InferenceDecisionRouter
from app.tool_use.service import ToolService
from tests.external_mcp_test_support import manifest
from tests.integration.test_external_mcp_real_servers_integration import everything_http

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TOOL_USE_REAL_SERVICE_TESTS") != "true",
    reason="RUN_TOOL_USE_REAL_SERVICE_TESTS=true の明示時だけ実行する",
)


def test_real_llm_selects_published_filesystem_tool(tmp_path):
    package = (
        Path(os.environ["MCP_REAL_SERVER_ROOT"])
        / "node_modules/@modelcontextprotocol/server-filesystem/dist/index.js"
    )
    assert package.is_file(), "公開Filesystem Serverを準備してください"
    node = shutil.which("node")
    assert node
    allowed = tmp_path / "files"
    allowed.mkdir()
    sample = allowed / "sample.txt"
    sample.write_text("今日の展示テーマは青い折り紙です。", encoding="utf-8")
    config = manifest(connection_id="acceptance-filesystem")
    config["connection"]["stdio"] = {
        "command": node,
        "args": [str(package), str(allowed)],
    }
    env = routing_environment()
    inference = create_inference_runtime(env)
    try:

        async def run():
            connection = Connection.from_manifest(config)
            registry = Registry()
            registry.register(connection)
            bindings = BindingResolver()
            gate = ExecutionGate(registry, bindings=bindings)
            service = ToolService(
                gate,
                InferenceDecisionRouter(inference.router),
                Sanitizer(create_privacy_scanner(resolved_memory_policy().privacy)),
                bindings,
            )
            client = ExternalMCPClient(connection)
            async with client.connect(), gate.attach(connection.id, client):
                material = await service.run(
                    "miori",
                    "acceptance",
                    f"{sample} を読んで、今日の展示テーマを教えてください。",
                )
                assert material.direct_text is None, "Tool選択が回答に進めませんでした"
                assert any("青い折り紙" in str(r) for r in material.results), (
                    "実ファイル内容を取得していません"
                )
                assert material.sources
                print(
                    "real-llm=gemma4:e4b; transport=stdio; tool-selection/read=passed"
                )
                greeting = await service.run("miori", "greeting", "こんにちは。元気？")
                assert not greeting.results and greeting.direct_text is None
                print("greeting/no-call=passed")
                service.close()

        asyncio.run(run())
    finally:
        inference.close()


def routing_environment():
    return {
        **{
            key: value
            for key, value in os.environ.items()
            if key.startswith("INFERENCE_TARGET_")
        },
        "OLLAMA_BASE_URL": os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        "INFERENCE_TARGET_CHAT": "ollama/gemma4:e4b",
        "INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS": "12288",
        "INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS": "1024",
        "INFERENCE_TARGET_TOOL_ROUTING": "ollama/gemma4:e4b",
        "INFERENCE_TARGET_TOOL_ROUTING_MAX_INPUT_TOKENS": "12288",
        "INFERENCE_TARGET_TOOL_ROUTING_MAX_OUTPUT_TOKENS": "1024",
        "INFERENCE_TARGET_TOOL_ROUTING_TIMEOUT_SECONDS": "90",
    }


def test_real_llm_reads_http_resource(tmp_path):
    node = shutil.which("node")
    package = (
        Path(os.environ["MCP_REAL_SERVER_ROOT"])
        / "node_modules/@modelcontextprotocol/server-everything/dist/index.js"
    )
    assert node and package.is_file()
    inference = create_inference_runtime(routing_environment())
    try:
        with everything_http({"node": node, "everything": str(package)}, tmp_path) as (
            endpoint,
            _process,
        ):

            async def run():
                connection = Connection.from_manifest(
                    manifest(
                        connection_id="acceptance-http",
                        transport="streamable_http",
                        endpoint=endpoint,
                    )
                )
                registry = Registry()
                registry.register(connection)
                bindings = BindingResolver()
                gate = ExecutionGate(registry, bindings=bindings)
                service = ToolService(
                    gate,
                    InferenceDecisionRouter(inference.router),
                    Sanitizer(
                        create_privacy_scanner(resolved_memory_policy().privacy),
                        (endpoint,),
                    ),
                    bindings,
                )
                client = ExternalMCPClient(connection)
                async with client.connect(), gate.attach(connection.id, client):
                    material = await service.run(
                        "miori",
                        "acceptance",
                        "外部資料のstartup.mdを読んで、Everything Serverの起動方法を教えてください。",
                    )
                    assert material.direct_text is None, (
                        "Resource選択が回答に進めませんでした"
                    )
                    assert any("Server Launcher" in str(r) for r in material.results), (
                        "実Resourceを取得していません"
                    )
                    assert any(s["label"] == "startup.md" for s in material.sources)
                    print(
                        "real-llm=gemma4:e4b; transport=streamable-http; resource-selection/read=passed"
                    )
                    service.close()

            asyncio.run(run())
    finally:
        inference.close()

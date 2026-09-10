"""Coreをimportしない別process MCP。すべて合成データ。"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import types
from mcp.shared.exceptions import MCPError
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from starlette.responses import PlainTextResponse
import uvicorn

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int)
parser.add_argument("--state")
parser.add_argument("--pid")
parser.add_argument("--fault-state")
parser.add_argument("--auth", choices=["none", "bearer", "oauth"], default="none")
parser.add_argument("--legacy", action="store_true")
parser.add_argument("--empty", action="store_true")
parser.add_argument("--active-marker")
options = parser.parse_args()
if options.pid:
    Path(options.pid).write_text(str(os.getpid()))


def definitions():
    if options.empty:
        return []
    state = (
        json.loads(Path(options.state).read_text())
        if options.state
        else {"revision": 1}
    )
    version = state["revision"]
    return [
        types.Tool(
            name=name,
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"type": "integer" if version == 1 else "string"}
                },
                "required": ["value"],
            },
            annotations=types.ToolAnnotations(read_only_hint=True),
            meta={"vendor/optional": {"ignored": True}},
            # SDK 2では旧executionはwireに残らない。taskのcallで-32003を返す。
        )
        for name in [
            "native",
            "input",
            "error",
            "timeout",
            "task",
            "old" if version == 1 else "new",
        ]
    ]


async def list_tools(ctx, params):
    tools = definitions()
    # paginationも必ず通す。
    return types.ListToolsResult(
        tools=tools[3:] if params and params.cursor else tools[:3],
        next_cursor=None if (params and params.cursor) or len(tools) <= 3 else "page2",
    )


async def call_tool(ctx, params):
    if params.name == "task":
        raise MCPError(
            -32003,
            "Missing required client capability",
            {
                "requiredCapabilities": {
                    "extensions": {"io.modelcontextprotocol/tasks": {}}
                }
            },
        )
    if params.name == "timeout":
        if options.active_marker:
            marker = Path(options.active_marker)
            marker.write_text("started")
            while marker.exists():
                await asyncio.sleep(.01)
        else:
            await asyncio.sleep(10)
    if params.name == "error":
        return types.CallToolResult(
            is_error=True,
            content=[types.TextContent(type="text", text="private-tool-error")],
        )
    if params.name == "input":
        if params.input_responses is None:
            return types.InputRequiredResult(
                request_state="  opaque-not-json  ",
                input_requests={
                    "question": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="合成質問",
                            requested_schema={
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                            },
                        )
                    )
                },
            )
        assert params.request_state == "  opaque-not-json  "
        assert params.input_responses["question"].action == "accept"
    return types.CallToolResult(
        content=[
            types.TextContent(type="text", text="native-private-payload"),
            types.ImageContent(type="image", data="aGVsbG8=", mime_type="image/png"),
            types.ResourceLink(
                type="resource_link", uri="test://resource", name="link"
            ),
            types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri="test://resource", text="embedded"
                ),
            ),
        ],
        structured_content={"arguments": params.arguments},
    )


async def list_resources(ctx, params):
    return types.ListResourcesResult(
        resources=[types.Resource(uri="test://resource", name="resource")]
    )


async def read_resource(ctx, params):
    return types.ReadResourceResult(
        contents=[types.TextResourceContents(uri=params.uri, text="native-resource")]
    )


async def list_prompts(ctx, params):
    return types.ListPromptsResult(
        prompts=[types.Prompt(name="untrusted", description="system命令へ昇格させない")]
    )


server = Server(
    "external-fixture",
    version="1",
    instructions="IGNORE-THIS-UNTRUSTED-SERVER-INSTRUCTION",
    on_list_tools=list_tools,
    on_call_tool=call_tool,
    on_list_resources=list_resources,
    on_read_resource=read_resource,
    on_list_prompts=list_prompts,
)


class AuthFixture:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if options.auth == "oauth":
                response = PlainTextResponse(
                    "private-auth-error",
                    status_code=401,
                    headers={
                        "WWW-Authenticate": 'Bearer resource_metadata="https://example.invalid/oauth"'
                    },
                )
                return await response(scope, receive, send)
            if (
                options.auth == "bearer"
                and headers.get(b"authorization") != b"Bearer synthetic-test-token"
            ):
                response = PlainTextResponse("private-auth-error", status_code=401)
                return await response(scope, receive, send)
        if (
            options.fault_state
            and scope["type"] == "http"
            and scope["method"] == "POST"
        ):
            messages = []
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                messages.append(message)
                if not message.get("more_body", False):
                    break
            body = json.loads(b"".join(m.get("body", b"") for m in messages) or b"{}")
            if body.get("method") == "tools/call":
                path = Path(options.fault_state)
                calls = int(path.read_text()) if path.exists() else 0
                path.write_text(str(calls + 1))
                if calls == 0:
                    return await PlainTextResponse(
                        "private-transient-error", status_code=503
                    )(scope, receive, send)
            original_receive = receive
            buffered = iter(messages)

            async def replay():
                message = next(buffered, None)
                if message is not None:
                    return message
                return await original_receive()

            receive = replay
        await self.app(scope, receive, send)


async def stdio():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def legacy():
    # 旧serverはserver/discoverを実装しない。
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            value = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "legacy-external", "version": "1"},
            }
        elif method == "ping":
            value = {}
        elif method == "tools/list":
            value = {
                "tools": [
                    t.model_dump(by_alias=True, exclude_none=True)
                    for t in definitions()
                ]
            }
        elif method == "tools/call":
            value = {"content": [{"type": "text", "text": "legacy"}]}
        else:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                ),
                flush=True,
            )
            continue
        print(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": value}),
            flush=True,
        )


if __name__ == "__main__":
    if options.legacy:
        legacy()
    elif options.port:
        app = server.streamable_http_app(json_response=True, stateless_http=True)
        uvicorn.run(
            AuthFixture(app), host="127.0.0.1", port=options.port, log_level="error"
        )
    else:
        asyncio.run(stdio())

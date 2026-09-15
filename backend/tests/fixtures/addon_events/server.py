"""Coreをimportしない、test-ownedのcursor履歴MCP。合成データのみ。"""
import argparse
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ResourceUpdated
import uvicorn

parser = argparse.ArgumentParser()
parser.add_argument("--state", required=True)
parser.add_argument("--calls", required=True)
parser.add_argument("--pid", required=True)
parser.add_argument("--port", type=int)
options = parser.parse_args()
Path(options.pid).write_text(str(os.getpid()))
bus = InMemorySubscriptionBus()


def state():
    return json.loads(Path(options.state).read_text())


def record(method):
    with Path(options.calls).open("a") as stream:
        stream.write(json.dumps({"method": method}) + "\n")


def cursor(epoch, pos):
    return f"{epoch}~{pos}"


def snapshot(data):
    return {"epoch": data["epoch"], "cursor": cursor(data["epoch"], data["position"]),
            "position": data["position"], "state": {"status": "current", "count": data["position"],
                                                   "body": "synthetic-private-body"}}


def history(data, arguments):
    epoch = data["epoch"]
    try:
        old_epoch, pos = arguments["cursor"].rsplit("~", 1)
        pos = int(pos)
    except (KeyError, ValueError):
        old_epoch, pos = "", -1
    gap = old_epoch != epoch or pos < data.get("floor", 0) or pos > data["position"]
    end = data["position"] if gap else min(data["position"], pos + arguments["limit"])
    events = [] if gap else [
        {"position": n, "cursor": cursor(epoch, n), "id": f"event-{n}", "type": "task.completed",
         "occurred_at": "2026-09-16T00:00:00+00:00",
         "metadata": {"task_ref": f"task-{n}", "status": "succeeded",
                      "body": "synthetic-private-body", "endpoint": "https://private.invalid/mcp",
                      "resource_ref": data.get("secret", "safe-ref")}}
        for n in range(pos + 1, end + 1)
    ]
    mode = data.get("mode")
    if mode == "duplicate" and events:
        events = list(reversed(events)) + [events[0]]
    if mode == "stale" and pos > 0:
        events.insert(0, {"position": pos, "cursor": cursor(epoch, pos), "body": "ignored"})
    if mode == "invalid_event" and events:
        events[0]["type"] = {"secret": "synthetic-private-body"}
    if mode == "unsafe_cursor" and events:
        events[0]["cursor"] = "https://private.invalid/secret"
    if mode == "missing_position" and events:
        events.pop(0)
    if mode == "id_conflict" and len(events) > 1:
        events[1]["id"] = events[0]["id"]
    if mode == "position_conflict" and events:
        events += [{**events[0], "id": "different"}]
    return {"epoch": epoch, "next_cursor": cursor(epoch, end), "position": end,
            "events": events, "more": end < data["position"], "gap": gap}


async def list_tools(ctx, params):
    version = state().get("definition", 1)
    return types.ListToolsResult(tools=[
        types.Tool(name=name, description=f"cursor profile {version}",
                   input_schema={"type": "object", "properties": {
                       "cursor": {"type": "string"}, "limit": {"type": "integer"},
                       "target": {"type": "string"}},
                       "required": ["cursor", "limit"] if name == "history" else [],
                       "additionalProperties": False},
                   annotations=types.ToolAnnotations(read_only_hint=True))
        for name in ("snapshot", "history")
    ])


async def call_tool(ctx, params):
    record(params.name)
    data = state()
    if data.get("delay"):
        await asyncio.sleep(data["delay"])
    if data.get("mode") == "error":
        return types.CallToolResult(is_error=True, content=[types.TextContent(type="text", text="synthetic-private-body")])
    if data.get("mode") == "broken_snapshot" and params.name == "snapshot":
        result = {"state": {}}
    else:
        result = snapshot(data) if params.name == "snapshot" else history(data, params.arguments)
    return types.CallToolResult(content=[], structured_content=result)


async def list_resources(ctx, params):
    return types.ListResourcesResult(resources=[
        types.Resource(uri=f"events://fixture/{name}", name=name)
        for name in ("snapshot", "history", "wake")
    ])


async def read_resource(ctx, params):
    parts = urlsplit(str(params.uri))
    name = parts.path.strip("/")
    record(name)
    args = {key: values[0] for key, values in parse_qs(parts.query).items()}
    if "limit" in args:
        args["limit"] = int(args["limit"])
    data = state()
    result = snapshot(data) if name != "history" else history(data, args)
    return types.ReadResourceResult(contents=[
        types.TextResourceContents(uri=params.uri, text=json.dumps(result))
    ])


@asynccontextmanager
async def lifespan(server):
    async def notify():
        previous = None
        while True:
            data = state()
            revision = (data["epoch"], data["position"])
            if revision != previous and data.get("wake", False):
                await bus.publish(ResourceUpdated(uri="events://fixture/wake"))
            previous = revision
            await asyncio.sleep(.02)
    task = asyncio.create_task(notify())
    try:
        yield {}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


server = Server("event-conformance", version="1", lifespan=lifespan,
                on_list_tools=list_tools, on_call_tool=call_tool,
                on_list_resources=list_resources, on_read_resource=read_resource,
                on_subscriptions_listen=ListenHandler(bus))


async def stdio():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    if options.port:
        uvicorn.run(server.streamable_http_app(stateless_http=True),
                    host="127.0.0.1", port=options.port, log_level="error")
    else:
        asyncio.run(stdio())

"""Minimal stdio MCP server (stdlib only) exposing one dummy tool for permission checks."""
import json, sys

def reply(id_, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}) + "\n"); sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    m, id_ = msg.get("method"), msg.get("id")
    if m == "initialize":
        reply(id_, {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "dummy-core", "version": "0"}})
    elif m == "tools/list":
        reply(id_, {"tools": [{"name": "core_echo", "description": "Echo text back (dummy Core tool).",
                               "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]})
    elif m == "tools/call":
        reply(id_, {"content": [{"type": "text", "text": "core_echo:" + msg["params"]["arguments"].get("text", "")}]})
    elif id_ is not None:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": "not found"}}) + "\n"); sys.stdout.flush()

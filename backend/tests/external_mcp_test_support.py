"""外部MCPの合成設定。実credentialは使用しない。"""

from app.external_mcp import Connection, Discovery


def manifest(
    *,
    trusted=False,
    connection_id="external-test",
    transport="stdio",
    endpoint=None,
    auth=None,
    sharing=None,
    restrictions=(),
    binding=False,
    budget=None,
):
    connection = {
        "id": connection_id,
        "ownership": "external",
        "protocol": "mcp",
        "transport": transport,
        "auth": auth or {"type": "none"},
        "trust": {"annotations": trusted, "addon_metadata": False},
        "grant": {"scope": "validated_snapshot", "activation": "next_execution_loop"},
    }
    if transport == "stdio":
        connection["stdio"] = {"command": "test-mcp", "args": []}
    else:
        connection["endpoint"] = endpoint or "http://127.0.0.1:9100/mcp"
    return {
        "manifest_version": "1.0",
        "connection": connection,
        "capabilities": {
            "source": "mcp_discovery",
            "tools": [],
            "resources": [],
            "prompts": [],
        },
        "core_policy": {
            "enabled": True,
            "sharing": sharing or {"mode": "shared"},
            "resource_binding_required": binding,
            "restrictions": list(restrictions),
            "execution_budget": budget
            or {
                "max_calls_per_loop": 6,
                "max_consecutive_same_tool": 3,
                "max_identical_call": 2,
                "normal_max_auto_cycles": 3,
            },
        },
    }


def discovery(*names):
    return Discovery(
        "2026-07-28",
        tuple(
            {
                "name": n,
                "description": "固有分類へ変換しない能力",
                "inputSchema": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "annotations": {"readOnlyHint": True},
                "_meta": {"vendor/optional": "native"},
            }
            for n in (names or ("native-tool",))
        ),
        ({"uri": "test://resource", "name": "resource"},),
        ({"name": "untrusted-prompt"},),
    )


class FakeSource:
    connected = True

    def __init__(self, connection: Connection, data=None):
        self.connection = connection
        self.data = data or discovery()
        self.calls = []
        self.failures = []
        self.result = {
            "content": [{"type": "text", "text": "native"}],
            "structuredContent": {"value": 1},
        }

    async def discover(self):
        return self.data

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        if self.failures:
            raise self.failures.pop(0)
        return self.result.copy()

    async def read_resource(self, uri, **kwargs):
        self.calls.append((uri, {}, kwargs))
        return {"contents": [{"uri": uri, "text": "resource native"}]}

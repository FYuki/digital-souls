"""検証で共有する最小の実行環境・合成応答。テスト収集に依存しない。"""

import json
from contextlib import asynccontextmanager
from app.external_mcp import Connection, ExecutionGate, Registry
from app.tool_use.binding import BindingResolver
from app.tool_use.projection import Sanitizer
from app.tool_use.routing import ToolDecision
from app.tool_use.service import ToolService
from app.privacy.contracts import ScanSuccess
from tests.external_mcp_test_support import FakeSource, manifest


class Scanner:
    def scan(self, text):
        return ScanSuccess(())


class Decisions:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.contexts = []

    async def decide(self, context, cancellation):
        self.contexts.append(context)
        step = self.steps.pop(0)
        return step(context) if callable(step) else step


def call(context, *, name="native-tool", value=1, binding=""):
    candidate = next(c for c in context["candidates"] if c["name"] == name)
    return ToolDecision("call", candidate["id"], json.dumps({"value": value}), binding)


@asynccontextmanager
async def runtime(
    decisions,
    *,
    config=None,
    targets=(),
    source_type=FakeSource,
    sanitizer=None,
    timeout=600,
):
    connection = Connection.from_manifest(config or manifest())
    registry = Registry()
    registry.register(connection)
    bindings = BindingResolver(targets)
    gate = ExecutionGate(registry, bindings=bindings)
    source = source_type(connection)
    service = ToolService(
        gate,
        decisions,
        sanitizer or Sanitizer(Scanner()),
        bindings,
        input_timeout=timeout,
    )
    async with gate.attach(connection.id, source):
        try:
            yield service, source, gate
        finally:
            service.close()


class InputSource(FakeSource):
    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        if kwargs.get("input_responses") is not None:
            return {"content": [{"type": "text", "text": "完了"}]}
        return {
            "resultType": "input_required",
            "requestState": "opaque-private-state",
            "inputRequests": {
                "answer": {
                    "method": "elicitation/create",
                    "params": {
                        "message": "好きな色を教えて",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {"color": {"type": "string"}},
                            "required": ["color"],
                            "additionalProperties": False,
                        },
                    },
                }
            },
        }


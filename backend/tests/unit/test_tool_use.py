"""#182: transport共通の選択・停止・入力待ちとnative正本の境界。"""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from app.external_mcp import Connection, ExecutionContext, ExecutionGate, Registry
from app.external_mcp.models import MCPFailure, encode
from app.tool_use.binding import BindingResolver, BindingTarget
from app.tool_use.catalog import catalog, select_candidates
from app.tool_use.projection import Sanitizer, bounded_json
from app.tool_use.routing import ToolDecision
from app.tool_use.service import ToolService
from app.privacy.contracts import ScanSuccess
from tests.external_mcp_test_support import FakeSource, discovery, manifest


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


def test_unclassified_tool_and_native_result_are_not_replaced():
    async def run():
        decisions = Decisions(call, ToolDecision("finish"))
        async with runtime(decisions) as (service, source, gate):
            original = encode(source.result)
            material = await service.run("miori", "session", "native-toolを使って")
            assert material.results[0]["structured"] == {"value": 1}
            assert material.sources[0]["snapshot_revision"].startswith("sha256:")
            assert encode(source.result) == original
            assert len(source.calls) == 1
            assert not gate._loops

    asyncio.run(run())


def test_greeting_does_not_invoke():
    async def run():
        async with runtime(Decisions(ToolDecision("finish"))) as (service, source, _):
            result = await service.run("miori", "session", "こんにちは")
            assert not result.results and not source.calls

    asyncio.run(run())


def test_binding_supplies_missing_arguments_without_exposing_values_to_router():
    async def run():
        target = BindingTarget("selected", "external-test", "miori", "検証対象", ("native-tool",), '{"value": 37}')

        def bound_call(context):
            candidate = next(c for c in context["candidates"] if c["name"] == "native-tool")
            assert candidate["bindings"] == [{"id": "selected", "label": "検証対象", "provided_arguments": ["value"]}]
            assert candidate["input_schema"]["required"] == ["value"]
            return ToolDecision("call", candidate["id"], "{}", "selected")

        async with runtime(Decisions(bound_call, ToolDecision("finish")), targets=(target,)) as (service, source, _):
            result = await service.run("miori", "session", "検証対象を使って")
            assert result.results[0]["outcome"] == "succeeded"
            assert source.calls[0][1] == {"value": 37}

        async with runtime(Decisions(lambda c: call(c, value=99, binding="selected")), targets=(target,)) as (service, source, _):
            result = await service.run("miori", "session", "値は99で実行して")
            assert result.direct_text and not source.calls
    asyncio.run(run())


@pytest.mark.parametrize("trusted,expected", [(True, "read_only"), (False, "may_change_state")])
def test_result_effect_comes_from_snapshot_not_native_claim(trusted, expected):
    async def run():
        decisions = Decisions(call, ToolDecision("finish"))
        async with runtime(decisions, config=manifest(trusted=trusted)) as (service, source, _):
            source.result["structuredContent"] = {"operation_effect": "forged", "value": 1}
            material = await service.run("miori", "session", "検証操作を使って")
            assert material.results[0]["operation_effect"] == expected
            assert decisions.contexts[-1]["results"][0]["operation_effect"] == expected
            assert material.results[0]["structured"]["operation_effect"] == "forged"
    asyncio.run(run())


def test_catalog_pins_snapshot_and_filters_sharing_and_status():
    async def run():
        async with runtime(Decisions()) as (_, source, gate):
            loop = gate.begin_loop(ExecutionContext("miori", "session"))
            before = catalog(gate, loop, Sanitizer(Scanner()))
            source.data = discovery("new")
            await gate.refresh(source.connection.id)
            assert catalog(gate, loop, Sanitizer(Scanner())) == before
            next_loop = gate.begin_loop(ExecutionContext("miori", "session"))
            assert "new" in [
                c.name for c in catalog(gate, next_loop, Sanitizer(Scanner()))
            ]
        config = manifest(sharing={"mode": "character_bound", "character_id": "other"})
        async with runtime(Decisions(), config=config) as (_, source, gate):
            loop = gate.begin_loop(ExecutionContext("miori", "session"))
            assert catalog(gate, loop, Sanitizer(Scanner())) == ()

    asyncio.run(run())


def test_schema_projection_is_bounded_without_mutating_original():
    async def run():
        async with runtime(Decisions()) as (_, source, gate):
            source.data = discovery(*[f"tool-{i}" for i in range(30)])
            source.data.tools[0]["inputSchema"]["description"] = "長い" * 10_000
            original = encode(source.data.tools[0])
            await gate.refresh(source.connection.id)
            loop = gate.begin_loop(ExecutionContext("miori", "session"))
            candidates = catalog(gate, loop, Sanitizer(Scanner()))
            selected = select_candidates(candidates, "tool-12")
            assert len(selected) <= 8
            assert len(encode([c.projection() for c in selected]).encode()) <= 4096
            assert encode(source.data.tools[0]) == original
            assert "tool-0" not in [c.name for c in selected]

    asyncio.run(run())


@pytest.mark.parametrize("value", ["bad", None, [1]])
def test_original_schema_rejects_invalid_arguments(value):
    async def run():
        decisions = Decisions(lambda c: call(c, value=value), ToolDecision("finish"))
        async with runtime(decisions) as (service, source, _):
            result = await service.run("miori", "session", "取得")
            assert not source.calls
            assert result.results[0]["error"] == "validation"

    asyncio.run(run())


def test_resource_is_read_via_gate():
    async def run():
        def read(c):
            item = next(i for i in c["candidates"] if i["kind"] == "resource")
            # Resourceには引数がない。モデルの補助欄を実行URIへ流用しない。
            return ToolDecision("call", item["id"], '{"uri":"unrelated://injected"}')

        async with runtime(Decisions(read, ToolDecision("finish"))) as (
            service,
            source,
            _,
        ):
            result = await service.run("miori", "session", "資料を取得")
            assert source.calls[0][0] == "test://resource"
            assert result.results[0]["text"] == ["resource native"]

    asyncio.run(run())


def test_result_secrets_errors_and_non_text_are_projected():
    sanitizer = Sanitizer(Scanner(), ("test-secret-123",))
    result = sanitizer.result(
        {
            "outcome": "succeeded",
            "native_payload": {
                "content": [
                    {
                        "type": "text",
                        "text": "test-secret-123 https://example.invalid/auth",
                    },
                    {"type": "image", "data": "raw"},
                ],
                "structuredContent": {
                    "password": "sensitive",
                    "nested": {"endpoint": "raw"},
                },
            },
        }
    )
    serialized = encode(result)
    assert all(
        s not in serialized
        for s in ("test-secret-123", "example.invalid", "sensitive", "raw")
    )
    assert result["omitted"]
    failure = sanitizer.result(
        {
            "outcome": "failed",
            "error_category": "auth",
            "native_payload": {"text": "secret"},
        }
    )
    assert failure == {"outcome": "failed", "error": "auth"}
    bounded = bounded_json({"text": '引用"\\日本語' * 10_000}, 512)
    assert len(bounded.encode()) <= 512 and json.loads(bounded)["omitted"]
    large = json.loads(
        bounded_json({"outcome": "succeeded", "text": "資料" * 1000}, 512)
    )
    assert large["outcome"] == "succeeded" and large["omitted"]


def test_arguments_allow_public_urls_but_reject_secrets_before_dispatch(monkeypatch):
    sanitizer = Sanitizer(Scanner(), ("known-private-value",), ("DS_TEST_MCP_TOKEN",))
    assert sanitizer.arguments_allowed({"url": "https://example.invalid/article"})
    assert not sanitizer.arguments_allowed({"text": "known-private-value"})
    assert not sanitizer.arguments_allowed({"nested": {"authorization": "value"}})
    monkeypatch.setenv("DS_TEST_MCP_TOKEN", "rotated-private-value")
    assert not sanitizer.arguments_allowed({"value": "rotated-private-value"})


def test_failed_unknown_operation_cannot_be_retried_by_routing():
    async def run():
        async with runtime(Decisions(call, call)) as (service, source, _):
            source.failures = [MCPFailure("transport", "connection_lost")]
            result = await service.run("miori", "session", "実行")
            assert len(source.calls) == 1
            assert result.results[0]["outcome"] == "result_unknown"

    asyncio.run(run())


def test_refresh_requires_user_request_and_preserves_old_snapshot():
    async def run():
        def refresh(c):
            return ToolDecision("refresh", c["candidates"][0]["id"])

        async with runtime(Decisions(refresh)) as (service, source, gate):
            result = await service.run("miori", "session", "こんにちは")
            assert result.direct_text and not source.calls and not gate._loops
        async with runtime(Decisions(refresh, ToolDecision("finish"))) as (
            service,
            source,
            gate,
        ):
            await service.run("miori", "session", "MCP一覧を更新")
            assert not gate._loops

    asyncio.run(run())


def test_binding_selection_resumes_original_arguments_and_same_budget():
    async def run():
        targets = tuple(
            BindingTarget(i, "external-test", "miori", i, ("native-tool",))
            for i in ("first", "second")
        )
        decisions = Decisions(
            call, lambda c: call(c, binding="second", value=999), ToolDecision("finish")
        )
        async with runtime(
            decisions, config=manifest(binding=True), targets=targets
        ) as (service, source, gate):
            question = await service.run("miori", "session", "操作して")
            assert question.waiting and not source.calls
            old_loop = next(iter(gate._loops))
            result = await service.run("miori", "session", "second")
            assert source.calls[0][1] == {"value": 1}
            assert result.results[0]["outcome"] == "succeeded"
            assert old_loop not in gate._loops

    asyncio.run(run())


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


def test_mrtr_resumes_same_loop_and_opaque_state_never_reaches_llm():
    async def run():
        decisions = Decisions(
            call,
            ToolDecision("clarify", instruction="好きな色を教えてください。"),
            ToolDecision(
                "resume",
                input_response_json='{"answer":{"action":"accept","content":{"color":"青"}}}',
            ),
            ToolDecision("finish"),
        )
        async with runtime(decisions, source_type=InputSource) as (
            service,
            source,
            gate,
        ):
            first = await service.run("miori", "session", "操作して")
            assert first.waiting
            loop = next(iter(gate._loops.values()))
            assert loop.calls == 1
            result = await service.run("miori", "session", "青")
            assert loop.calls == 2
            assert result.results[0]["outcome"] == "succeeded"
            assert source.calls[1][2]["request_state"] == "opaque-private-state"
            assert "opaque-private-state" not in encode(decisions.contexts)
            assert not gate._pending and not gate._loops

    asyncio.run(run())


def test_expiration_releases_pending_gate_state():
    async def run():
        async with runtime(
            Decisions(call, ToolDecision("clarify", instruction="色は？")),
            source_type=InputSource,
            timeout=0.01,
        ) as (service, source, gate):
            await service.run("miori", "session", "操作して")
            assert gate._pending
            await asyncio.sleep(0.03)
            assert not gate._pending and not gate._loops

    asyncio.run(run())


def test_stop_during_dispatch_prevents_followup_and_late_reply():
    async def run():
        entered = asyncio.Event()

        class Slow(FakeSource):
            async def call_tool(self, *args, **kwargs):
                entered.set()
                await asyncio.Event().wait()

        async with runtime(Decisions(call), source_type=Slow) as (service, _, gate):
            task = asyncio.create_task(service.run("miori", "session", "実行"))
            await entered.wait()
            service.stop("miori", "session")
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not gate._loops and not gate._pending

    asyncio.run(run())

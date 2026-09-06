"""Tool利用の予算・停止・会話分離・設定の境界を検証する。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.chat_service import ChatInputLimitError
from app.external_mcp.models import encode
from app.inference import (
    InferenceCancellationToken,
    InferenceError,
    InferenceErrorCategory,
)
from app.prompting import BuiltPrompt, PromptMessage, PromptRole, PromptUsage
from app.tool_use.prompt import require_tool_room, with_tool_material
from app.tool_use.routing import InferenceDecisionRouter, ToolDecision
from app.tool_use.runtime import ToolSettings
from app.tool_use.service import ToolMaterial
from app.tool_use.binding import BindingResolver, BindingTarget
from app.external_mcp.models import MCPFailure
from tests.unit.test_tool_use import Decisions, InputSource, call, runtime


def test_configured_optional_binding_uses_unique_target_and_asks_on_ambiguity():
    one = BindingTarget(
        "a", "connection", "miori", "対象A", ("operation",), '{"project":"a"}'
    )
    two = BindingTarget(
        "b", "connection", "miori", "対象B", ("operation",), '{"project":"b"}'
    )
    resolver = BindingResolver((one,))
    binding, arguments = resolver.resolve("miori", "session", "connection", "operation")
    assert binding and arguments == {"project": "a"}
    resolver = BindingResolver((one, two))
    with pytest.raises(MCPFailure, match="binding_input_required"):
        resolver.resolve("miori", "session", "connection", "operation")


def test_per_call_binding_overrides_legacy_context_and_is_pinned_across_mrtr():
    from app.external_mcp import Connection, ExecutionContext, ExecutionGate, Registry
    from tests.external_mcp_test_support import manifest

    class Binding:
        seen = []

        async def validate(self, connection, operation, character, binding, session):
            self.seen.append(binding)
            return binding == "call-binding"

    async def run():
        connection = Connection.from_manifest(manifest(binding=True))
        registry = Registry()
        registry.register(connection)
        validator = Binding()
        gate = ExecutionGate(registry, bindings=validator)
        source = InputSource(connection)
        async with gate.attach(connection.id, source):
            loop = gate.begin_loop(
                ExecutionContext("miori", "a", binding_id="old-context-binding")
            )
            pending = await gate.invoke(
                connection.id,
                "native-tool",
                {"value": 1},
                loop,
                binding_id="call-binding",
            )
            assert pending["outcome"] == "input_required"
            result = await gate.resume(
                pending["interaction_id"],
                {"answer": {"action": "accept", "content": {"color": "青"}}},
                loop,
            )
            assert result["outcome"] == "succeeded"
            assert validator.seen == ["call-binding"] * 4
            assert gate._loops[loop].calls == 2
            gate.end_loop(loop)

    asyncio.run(run())


def test_binding_is_revalidated_after_connection_queue_wait():
    from app.external_mcp import Connection, ExecutionContext, ExecutionGate, Registry
    from tests.external_mcp_test_support import FakeSource, manifest

    async def run():
        entered = asyncio.Event()

        class Binding:
            valid = True

            async def validate(self, *args):
                entered.set()
                return self.valid

        connection = Connection.from_manifest(manifest(binding=True))
        registry = Registry()
        registry.register(connection)
        validator = Binding()
        gate = ExecutionGate(registry, bindings=validator)
        source = FakeSource(connection)
        async with gate.attach(connection.id, source):
            loop = gate.begin_loop(ExecutionContext("miori", "a"))
            async with gate._locks[connection.id].hold(False):
                task = asyncio.create_task(
                    gate.invoke(
                        connection.id,
                        "native-tool",
                        {"value": 1},
                        loop,
                        binding_id="binding",
                    )
                )
                await entered.wait()
                validator.valid = False
            result = await task
            assert (
                result["outcome"] == "failed"
                and result["error_category"] == "policy"
                and not source.calls
            )
            gate.end_loop(loop)

    asyncio.run(run())


def test_stop_cancels_final_answer_after_tool_loop_ended():
    async def run():
        async with runtime(Decisions(call, ToolDecision("finish"))) as (
            service,
            _,
            gate,
        ):
            generating = asyncio.Event()

            async def reply():
                with service.response_scope("miori", "a"):
                    await service.run("miori", "a", "取得")
                    generating.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(reply())
            await generating.wait()
            assert not gate._loops
            assert service.status("miori", "a")["state"] == "running"
            service.stop("miori", "a")
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not service._owners

    asyncio.run(run())


def test_partial_success_survives_later_routing_failure():
    async def run():
        async with runtime(
            Decisions(call, ToolDecision("call", "not-a-candidate"))
        ) as (service, _, _):
            result = await service.run("miori", "a", "取得")
            assert result.direct_text is None
            assert result.results[0]["outcome"] == "succeeded"
            assert result.results[-1]["outcome"] == "incomplete"

    asyncio.run(run())


def test_successful_identical_call_is_not_repeated():
    async def run():
        async with runtime(Decisions(call, call)) as (service, source, _):
            result = await service.run("miori", "a", "取得")
            assert len(source.calls) == 1
            assert result.results[0]["outcome"] == "succeeded"

    asyncio.run(run())


def test_mrtr_schema_allows_only_current_state_and_original_question():
    from app.tool_use.routing import InferenceDecisionRouter

    answer = {"type": "object", "required": ["color"]}
    schema = InferenceDecisionRouter._schema(
        {"pending": {"answer_schema": answer}, "candidates": []}
    )
    assert set(schema["properties"]["action"]["enum"]) == {
        "resume",
        "clarify",
        "blocked",
    }
    assert schema["properties"]["input_response_json"]["anyOf"][0] == answer

    async def run():
        async with runtime(
            Decisions(
                call, ToolDecision("clarify", instruction="モデルの無関係な説明")
            ),
            source_type=InputSource,
        ) as (service, _, _):
            result = await service.run("miori", "a", "取得")
            assert "好きな色" in result.direct_text
            assert "無関係" not in result.direct_text

    asyncio.run(run())


def test_result_room_is_required_only_for_dispatch():
    async def run():
        async def reject():
            raise ChatInputLimitError("external_tool_results", 100, 99)

        async with runtime(Decisions(ToolDecision("finish"), call)) as (
            service,
            source,
            gate,
        ):
            await service.run("miori", "a", "こんにちは", before_execute=reject)
            with pytest.raises(ChatInputLimitError):
                await service.run("miori", "a", "取得", before_execute=reject)
            assert not source.calls and not gate._loops

    asyncio.run(run())


@pytest.mark.parametrize("reason", ["disconnect", "session_ended", "user_stop"])
def test_interruption_expires_pending_but_answer_barge_in_preserves_it(reason):
    async def run():
        async with runtime(
            Decisions(call, ToolDecision("clarify", instruction="好きな色は？")),
            source_type=InputSource,
        ) as (service, _, gate):
            await service.run("miori", "a", "取得")
            pending = tuple(gate._pending)
            service.interrupted("miori", "a", "barge_in")
            assert tuple(gate._pending) == pending
            service.interrupted("miori", "other", reason)
            assert tuple(gate._pending) == pending
            service.interrupted("miori", "a", reason)
            assert not gate._pending and not gate._loops

    asyncio.run(run())


def test_routing_reduces_history_and_candidates_before_generation():
    class Router:
        generated = None

        def estimate_input_tokens(self, **kwargs):
            context = json.loads(kwargs["messages"][-1].content)
            if context["history"] or len(context["candidates"]) > 1:
                raise InferenceError(
                    InferenceErrorCategory.INVALID_REQUEST, retryable=False
                )

        def generate_structured(self, **kwargs):
            self.generated = kwargs
            return SimpleNamespace(value={"action": "finish"})

    async def run():
        router = Router()
        context = {
            "history": ["old", "recent"],
            "candidates": [{"id": "first"}, {"id": "second"}],
        }
        await InferenceDecisionRouter(router).decide(
            context, InferenceCancellationToken()
        )
        sent = json.loads(router.generated["messages"][-1].content)
        assert sent == {"history": [], "candidates": [{"id": "first"}]}
        assert len(context["history"]) == 2
        assert router.generated["response_schema"]["properties"]["candidate_id"][
            "enum"
        ] == ["", "first"]

    asyncio.run(run())


def test_sufficient_result_finishes_before_selecting_another_operation():
    class Router:
        calls = 0

        def estimate_input_tokens(self, **kwargs):
            assert set(kwargs["response_schema"]["properties"]) == {"done"}

        def generate_structured(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(value={"done": True})

    async def run():
        router = Router()
        decision = await InferenceDecisionRouter(router).decide(
            {
                "original_request": "展示を準備",
                "current_user": "赤",
                "results": [{"outcome": "succeeded", "text": "赤い展示の準備完了"}],
                "candidates": [{"id": "tool"}],
                "history": [],
                "pending": None,
            },
            InferenceCancellationToken(),
        )
        assert decision.action == "finish" and router.calls == 1

    asyncio.run(run())


def test_prompt_shrinks_results_preserving_original_and_current_user():
    prompt = BuiltPrompt(
        (
            PromptMessage(PromptRole.SYSTEM, "人格"),
            PromptMessage(PromptRole.USER, "質問"),
        ),
        PromptUsage(*([0] * 10)),
        (),
    )
    counter = lambda messages: sum(len(m.content.encode()) for m in messages)
    material = ToolMaterial(results=({"outcome": "succeeded", "text": "資料" * 5000},))
    before = encode(material.results[0])
    require_tool_room(prompt, counter, 1600)
    result = with_tool_material(prompt, material, counter, 1600)
    assert counter(result.messages) <= 1600
    assert result.messages[-1] == prompt.messages[-1]
    assert (
        "omitted" in result.messages[-2].content
        or "省略" in result.messages[-2].content
    )
    assert encode(material.results[0]) == before
    assert with_tool_material(prompt, ToolMaterial(), counter, 1) is prompt
    small = with_tool_material(
        prompt,
        ToolMaterial(results=({"outcome": "succeeded", "text": "短い結果"},)),
        counter,
        1600,
    )
    assert "短い結果" in small.messages[-2].content


@pytest.mark.parametrize(
    "config",
    [
        {"version": True, "connections": []},
        {"version": 1, "connections": {}},
        {"version": 1, "connections": [], "bindings": [None]},
        {"version": 1, "connections": [], "extra": "raw-secret"},
    ],
)
def test_invalid_config_does_not_expose_values(tmp_path, config):
    path = tmp_path / "private-config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="^invalid DS_MCP_CONFIG$"):
        ToolSettings.load(str(path))

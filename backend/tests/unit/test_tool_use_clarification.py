"""実行前の確認Q&Aを、通常履歴の切り詰めや別会話から独立して扱う。"""

import asyncio
import json

from app.tool_use.routing import ToolDecision
from tests.unit.test_tool_use import Decisions, call, runtime


def test_answer_to_local_clarification_is_kept_without_conversation_history():
    async def scenario():
        question = "検索するキーワードを教えてください。"

        def answered(context):
            assert context["original_request"] == "気になった投稿を紹介して"
            assert context["current_user"] == "夕日や月光"
            assert context["clarification"] == [
                {"question": question, "answer": "光織の興味があること"},
                {"question": "具体的なテーマはありますか？", "answer": "夕日や月光"},
            ]
            assert (
                context["pending"] is None
            )  # MCPのresumeとは異なり、まだ操作していない。
            return call(context)

        decisions = Decisions(
            ToolDecision("clarify", instruction=question),
            ToolDecision("clarify", instruction="具体的なテーマはありますか？"),
            answered,
            ToolDecision("finish"),
        )
        async with runtime(decisions) as (service, source, gate):
            assert (await service.run("miori", "a", "気になった投稿を紹介して")).waiting
            first_loop = next(iter(gate._loops))
            assert (await service.run("miori", "a", "光織の興味があること")).waiting
            assert not source.calls
            assert next(iter(gate._loops)) == first_loop
            result = await service.run("miori", "a", "夕日や月光")
            assert not result.waiting and result.sources
            assert len(source.calls) == 1
            assert not gate._loops

    asyncio.run(scenario())


def test_local_clarification_does_not_leak_after_stop_or_to_another_conversation():
    async def scenario():
        def fresh(context):
            assert not context.get("clarification")
            return ToolDecision("finish")

        async with runtime(
            Decisions(ToolDecision("clarify", instruction="テーマは？"), fresh, fresh)
        ) as (service, source, _):
            await service.run("miori", "a", "投稿を探して")
            await service.run("miori", "b", "こんにちは")
            service.stop("miori", "a")
            await service.run("miori", "a", "こんにちは")
            assert not source.calls

    asyncio.run(scenario())


def test_local_clarification_wait_is_bounded_across_turns():
    async def scenario():
        decisions = Decisions(*[ToolDecision("clarify", instruction="テーマは？")] * 5)
        async with runtime(decisions) as (service, source, gate):
            for _ in range(4):
                assert (await service.run("miori", "a", "まだ決めていない")).waiting
            result = await service.run("miori", "a", "まだ決めていない")
            assert not result.waiting and result.direct_text
            assert not source.calls and not gate._loops

    asyncio.run(scenario())


def test_answer_is_retained_when_router_must_drop_ordinary_history():
    from types import SimpleNamespace

    from app.inference import (
        InferenceCancellationToken,
        InferenceError,
        InferenceErrorCategory,
    )
    from app.tool_use.routing import InferenceDecisionRouter

    qa = [{"question": "検索語は？", "answer": "折り紙"}]

    class Model:
        def estimate_input_tokens(self, **kwargs):
            context = json.loads(kwargs["messages"][-1].content)
            if context["history"]:
                raise InferenceError(
                    InferenceErrorCategory.INVALID_REQUEST, retryable=False
                )

        def generate_structured(self, **kwargs):
            context = json.loads(kwargs["messages"][-1].content)
            assert not context["history"]
            assert context["clarification"] == qa
            assert context["current_user"] == "折り紙"
            assert "clarification" in kwargs["messages"][0].content
            return SimpleNamespace(
                value={
                    "action": "call",
                    "candidate_id": "search",
                    "arguments_json": '{"keyword":"折り紙"}',
                }
            )

    decision = asyncio.run(
        InferenceDecisionRouter(Model()).decide(
            {
                "original_request": "投稿を検索して",
                "current_user": "折り紙",
                "clarification": qa,
                "history": [{"role": "assistant", "content": "古い質問"}],
                "candidates": [{"id": "search"}],
                "pending": None,
            },
            InferenceCancellationToken(),
        )
    )
    assert decision.action == "call" and decision.arguments() == {"keyword": "折り紙"}
    assert qa == [{"question": "検索語は？", "answer": "折り紙"}]


def test_switching_request_discards_previous_clarification():
    async def scenario():
        def fresh(context):
            assert context["original_request"] == "検索はやめて。こんにちは"
            assert not context["clarification"]
            return ToolDecision("finish")

        async with runtime(
            Decisions(
                ToolDecision("clarify", instruction="検索語は？"),
                ToolDecision("abandon"),
                fresh,
            )
        ) as (service, source, gate):
            await service.run("miori", "a", "投稿を探して")
            result = await service.run("miori", "a", "検索はやめて。こんにちは")
            assert not result.waiting and not source.calls and not gate._loops

    asyncio.run(scenario())

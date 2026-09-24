from __future__ import annotations

import asyncio

import pytest

from app.tool_use.routing import ToolDecision
from tests.tool_use_test_support import Decisions, call, runtime


def test_clarification_reopens_the_same_run_before_gate_dispatch():
    async def scenario() -> None:
        observed_loops: list[str] = []

        def answer(context):
            observed_loops.append(next(iter(gate._loops)))
            assert context["clarification"] == [
                {"question": "検索語は？", "answer": "夕日"}
            ]
            return call(context)

        decisions = Decisions(
            ToolDecision("clarify", instruction="検索語は？"),
            answer,
            ToolDecision("finish"),
        )
        async with runtime(decisions) as (service, source, gate):
            first = await service.run("miori", "session-1", "資料を探して")
            first_loop = next(iter(gate._loops))
            assert first.waiting

            result = await service.run("miori", "session-1", "夕日")

            assert not result.waiting
            assert len(source.calls) == 1
            assert observed_loops == [first_loop]
            assert decisions.contexts[1]["clarification"][-1]["answer"] == "夕日"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments_json",
    [
        "```json\n{\"value\":1}\n```",
        '{"value": 1,}',
        '{"value": 1',
        "[1]",
        '"value"',
    ],
)
def test_invalid_tool_arguments_never_reach_gate_dispatch(arguments_json: str):
    async def scenario() -> None:
        def invalid_call(context):
            candidate = next(
                item for item in context["candidates"] if item["name"] == "native-tool"
            )
            return ToolDecision(
                "call", candidate["id"], arguments_json=arguments_json
            )

        async with runtime(
            Decisions(invalid_call)
        ) as (service, source, _gate):
            result = await service.run("miori", "session-1", "取得")

            assert not result.waiting
            assert result.direct_text
            assert source.calls == []

    asyncio.run(scenario())

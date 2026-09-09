"""mainの外部ツール連携と音声品質観測の統合境界を検証する。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app import main
from app.conversation_core import ResponseState
from app.inference.diagnostics import collect_diagnostics
from app.prompting import BuiltPrompt, PromptMessage, PromptRole, PromptUsage
from app.tool_use.service import ToolMaterial
from tests.unit.test_conversation_core_stop_confirmation import HeldStop, make_session, start, tick


@pytest.mark.parametrize("callback_fails", [False, True])
def test_output_stop_notifies_tools_once_before_confirmation(callback_fails):
    async def exercise():
        notifications = []

        def interrupted(reason):
            notifications.append(reason)
            if callback_fails:
                raise RuntimeError("停止通知の失敗")

        stop = HeldStop()
        session, _, persistence, _ = make_session(stop, on_interruption=interrupted)
        assert await session.cancel_response(response_id="unknown", reason="barge_in") is None
        assert notifications == []
        await start(session)
        first = asyncio.create_task(session.cancel_response(response_id="r", reason="barge_in"))
        await asyncio.wait_for(stop.entered.wait(), 1)
        second = asyncio.create_task(session.cancel_response(response_id="r", reason="duplicate"))
        await tick()
        try:
            assert notifications == ["barge_in"]
            assert not first.done() and not second.done()
        finally:
            stop.release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 1)
            await session.end()
        assert all(result.state is ResponseState.CANCELLED for result in results)
        assert len(persistence.outcomes) == 1
        assert len(stop.calls) == 1

    asyncio.run(exercise())


def test_voice_diagnostics_describe_final_tool_and_life_prompt(monkeypatch):
    initial = BuiltPrompt(
        (PromptMessage(PromptRole.USER, "質問"),),
        PromptUsage(*([0] * 10)),
        (),
    )
    seen = []

    def life_context(_character, prompt):
        return replace(
            prompt,
            messages=(PromptMessage(PromptRole.SYSTEM, "状態"), *prompt.messages),
            usage=replace(prompt.usage, total=prompt.usage.total + 2),
        )

    class Tools:
        async def run(self, *_args, **_kwargs):
            return ToolMaterial(results=({"outcome": "succeeded", "text": "資料"},))

    async def stream(prompt, **_kwargs):
        seen.append(prompt)
        yield "応答"

    monkeypatch.setattr(main.llm_router, "stream_response", stream)
    monkeypatch.setattr(main.llm_router, "count_input_tokens", lambda messages, **_: sum(len(m.content) for m in messages))
    service = SimpleNamespace(
        prepare_unrecorded_generation=lambda *_: (initial, 100),
        with_life_context=life_context,
        record_successful_prompt_references=lambda _: None,
    )

    async def exercise():
        with collect_diagnostics() as collector:
            chunks = [chunk async for chunk in main._stream_core_reply(
                service, SimpleNamespace(chat_context_tokens=4000), "miori", None,
                "質問", tools=Tools(), conversation_id="conversation",
            )]
        assert chunks == ["応答"]
        events = {event.name: event for event in collector.finish()}
        assert len(seen) == 1 and len(seen[0].messages) > len(initial.messages)
        assert events["prompt_message_count"].value == len(seen[0].messages)
        assert events["prompt_input_tokens"].value == seen[0].usage.total

    asyncio.run(exercise())

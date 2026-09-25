"""mainの外部ツール連携と音声品質観測の統合境界を検証する。"""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import _chat_runtime, main
from app.conversation_core import ResponseState
from app.inference.diagnostics import collect_diagnostics
from app.model_settings import resolve_model_settings
from app.prompting import (
    BuiltPrompt,
    CharacterPrompt,
    PromptMessage,
    PromptRole,
    PromptUsage,
)
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
    captured = []

    def life_context(_character, prompt):
        return replace(
            prompt,
            messages=(PromptMessage(PromptRole.SYSTEM, "状態"), *prompt.messages),
            usage=replace(prompt.usage, total=prompt.usage.total + 2),
        )

    class Tools:
        async def run(self, *_args, before_execute=None, **_kwargs):
            if before_execute is not None:
                await before_execute()
            return ToolMaterial(results=({"outcome": "succeeded", "text": "資料"},))

    async def stream(prompt, **_kwargs):
        seen.append(prompt)
        yield "応答"

    settings = resolve_model_settings(
        {}, chat_context_tokens=4000, assistant_max_generation_tokens=100
    )
    service = _chat_runtime.ChatService(
        _chat_runtime.ChatRuntimeConfig(
            rag_enabled=False,
            memory_policy=None,
            prompt_config=settings,
            chroma_path=Path("/test/runtime-data/chroma"),
        ),
        SimpleNamespace(open_session=lambda *_: SimpleNamespace()),
        _chat_runtime.ChatRuntimeDependencies(
            character_definition_loader=(
                lambda _c: _chat_runtime.CharacterRuntimeDefinition(
                    prompt=CharacterPrompt("", "", "", "", "", ""),
                    character_book=None,
                )
            ),
            prompt_builder=lambda **_kwargs: initial,
            llm_response_generator=lambda *_a, **_k: "unused",
            input_token_counter=lambda messages: sum(
                len(message.content) for message in messages
            ),
            privacy_scanner=MagicMock(),
            semantic_classifier=MagicMock(),
            approved_memory_repository=MagicMock(),
            memory_embedder=lambda _text: [0.1],
            memory_formation_submitter=MagicMock(),
            life_context=life_context,
        ),
    )

    monkeypatch.setattr(main.llm_router, "stream_response", stream)
    # 旧経路はrouterの計測を直接呼ぶ。共通準備は注入済みcounterを使う。
    monkeypatch.setattr(
        main.llm_router,
        "count_input_tokens",
        lambda messages, **_: sum(len(m.content) for m in messages),
    )

    async def exercise():
        with collect_diagnostics() as collector:
            chunks = [chunk async for chunk in main._stream_core_reply(
                service, settings, "miori", None,
                "質問", tools=Tools(), conversation_id="conversation",
                prompt_observer=captured.append,
            )]
        assert chunks == ["応答"]
        events = {event.name: event for event in collector.finish()}
        assert len(seen) == 1 and len(seen[0].messages) > len(initial.messages)
        assert captured == seen
        assert events["prompt_message_count"].value == len(seen[0].messages)
        assert events["prompt_input_tokens"].value == seen[0].usage.total

    asyncio.run(exercise())

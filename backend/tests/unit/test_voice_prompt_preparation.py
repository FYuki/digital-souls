"""発話前の固定人格計測と、通常prompt・取消境界の契約。"""
import asyncio
from dataclasses import replace
import json
from threading import BoundedSemaphore, Event
from types import SimpleNamespace

import httpx
import pytest

from app import async_worker
from app._chat_runtime import ChatService, CharacterRuntimeDefinition
from app.inference.adapters.ollama import OllamaAdapter
from app.inference.authorization import InferenceCaller
from app.inference.cancellation import current_cancellation_token, raise_if_cancelled
from app.inference.contracts import InferenceMessage, InferenceTarget
from app.inference.errors import InferenceError, InferenceErrorCategory
from app.prompting import CharacterPrompt, MaskedHistory, PromptBuilder, RagContext
from tests.prompt_test_support import prompt_build_input
from tests.unit.test_inference_core import _router


CHARACTER = CharacterPrompt(
    description="  固定の概要  ", personality="", scenario="設定",
    system_prompt="応答方針", mes_example="", post_history_instructions=" 最終指示 ",
)


def service(counter, character=CHARACTER):
    # 準備に許された依存だけを渡す。履歴、検索、形成、生成へ触れれば失敗する。
    def load(character_id):
        assert character_id == "miori"
        return CharacterRuntimeDefinition(prompt=character, character_book=None)
    return ChatService(
        runtime_config=SimpleNamespace(rag_enabled=True, memory_policy=object()),
        conversation_history_service=object(),
        dependencies=SimpleNamespace(
            character_definition_loader=load, input_token_counter=counter, tools=None,
        ),
    )


def test_preparation_reuses_exact_counts_in_the_unchanged_normal_prompt():
    chat_requests = []
    digest = "a" * 64
    def handle(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:e4b", "digest": digest}]})
        assert request.url.path == "/api/chat"
        chat_requests.append(json.loads(request.content))
        return httpx.Response(200, json={"prompt_eval_count": 10})
    transport = httpx.MockTransport(handle)
    with httpx.Client(transport=transport) as client:
        adapter = OllamaAdapter(
            base_url="http://127.0.0.1:11434", http_client=client,
            async_client_factory=lambda seconds: httpx.AsyncClient(transport=transport, timeout=seconds),
        )
        router = _router(adapter)
        def count(messages):
            return router.estimate_input_tokens(
                caller=InferenceCaller.CHAT, target=InferenceTarget.CHAT,
                messages=tuple(InferenceMessage(m.role.value, m.content) for m in messages),
            ).count
        app_service = service(count)
        builder = PromptBuilder(SimpleNamespace(count_input_tokens=count))
        prompt_input = prompt_build_input(
            character=CHARACTER, rag=RagContext(items=()),
            history=MaskedHistory(turns=(), omitted_turns=0),
        )
        asyncio.run(app_service.prepare_character_input_tokens("miori", timeout_seconds=2))
        assert len(chat_requests) == 2
        assert all(len(r["messages"]) == 1 and r["messages"][0]["role"] == "system" for r in chat_requests)
        warm = builder.build(prompt_input)
        # 人格2要求はcache hit、当該userと全体の正確な計測だけを実行する。
        assert len(chat_requests) == 4
        assert chat_requests[2]["messages"] == [{"role": "user", "content": "現在user原文"}]
        assert all(r["options"]["num_predict"] == 1 for r in chat_requests)
        assert all(r["options"]["num_ctx"] == 9216 for r in chat_requests)
        assert warm.messages[0].content == "## キャラクター概要\n固定の概要\n\n## 関係と世界観\n設定\n\n## 応答方針\n応答方針"
        assert warm.messages[-2].content == "最終指示"
        # モデル変更時は事前計測を再利用しない。本文・予算判定は同じ。
        digest = "b" * 64
        cold = builder.build(prompt_input)
        assert len(chat_requests) == 8
        assert warm.messages == cold.messages
        assert warm.usage == cold.usage
        # 準備後の人格編集にも旧countを使わない。
        builder.build(replace(prompt_input, character=replace(CHARACTER, description="編集後")))
        assert len(chat_requests) == 10
        adapter.close()


def test_empty_character_does_not_make_a_count_request():
    empty = CharacterPrompt("", " ", "", "", "", " ")
    asyncio.run(service(lambda _: pytest.fail("空の人格は計測しない"), empty)
                .prepare_character_input_tokens("miori", timeout_seconds=1))


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_cancellation_stops_worker_before_post_history_and_returns_capacity(monkeypatch, mode):
    entered, finished = Event(), Event()
    calls = []
    capacity = BoundedSemaphore(1)
    monkeypatch.setattr(async_worker, "_CAPACITY", capacity)
    def count(messages):
        calls.append(messages)
        token = current_cancellation_token()
        assert token is not None
        entered.set()
        try:
            for _ in range(400):
                if token.is_cancelled:
                    raise_if_cancelled()
                finished.wait(0.005)
            pytest.fail("取消がworkerへ届かなかった")
        finally:
            finished.set()
    async def scenario():
        pending = asyncio.create_task(service(count).prepare_character_input_tokens(
            "miori", timeout_seconds=0.1 if mode == "timeout" else 3,
        ))
        assert await asyncio.to_thread(entered.wait, 1)
        if mode == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            with pytest.raises(InferenceError) as failure:
                await pending
            assert failure.value.category is InferenceErrorCategory.TIMEOUT
        assert await asyncio.to_thread(finished.wait, 1)
        # invokeのfinallyが枠を返し終えるまで待つ。
        assert await asyncio.to_thread(capacity.acquire, True, 1)
        capacity.release()
        assert len(calls) == 1
        assert current_cancellation_token() is None
        completed = []
        await service(lambda messages: completed.append(messages) or 10).prepare_character_input_tokens(
            "miori", timeout_seconds=1,
        )
        assert len(completed) == 2
    asyncio.run(scenario())

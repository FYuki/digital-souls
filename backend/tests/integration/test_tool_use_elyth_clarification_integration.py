"""実ELYTHと実LLMで、実行前の追加Q&Aから検索へ進む受入。"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from app.external_mcp import Connection, ExecutionGate, ExternalMCPClient, Registry
from app.external_mcp.models import encode
from app.inference.runtime import create_inference_runtime
from app.memory.memory_policy import resolved_memory_policy
from app.privacy.scanner import create_privacy_scanner
from app.tool_use.binding import BindingResolver
from app.tool_use.projection import Sanitizer
from app.tool_use.routing import InferenceDecisionRouter
from app.tool_use.service import ToolService
from tests.integration.test_tool_use_real_service_integration import routing_environment

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_TOOL_USE_ELYTH_REAL_TESTS") != "true",
    reason="RUN_TOOL_USE_ELYTH_REAL_TESTS=true とELYTH_API_KEYを明示した場合のみ実行",
)


def test_real_elyth_search_after_two_clarification_answers():
    key = os.environ.get("ELYTH_API_KEY")
    assert key, "ELYTH_API_KEYを環境で設定してください"
    config = json.loads(
        (
            Path(__file__).parents[2] / "config/character-life-elyth.example.json"
        ).read_text()
    )["connections"][0]
    # このケースは公開投稿検索のみ。未有効のイベント機能等へ寄り道しない。
    config["core_policy"]["operation_allowlist"] = ["search_post"]
    env = {
        **routing_environment(),
        "INFERENCE_TARGET_TOOL_ROUTING_OPTIONS_JSON": '{"temperature":0}',
    }
    inference = create_inference_runtime(env)

    async def scenario():
        connection = Connection.from_manifest(config)
        registry = Registry()
        registry.register(connection)
        bindings = BindingResolver()
        gate = ExecutionGate(registry, bindings=bindings)
        service = ToolService(
            gate,
            InferenceDecisionRouter(inference.router),
            Sanitizer(create_privacy_scanner(resolved_memory_policy().privacy), (key,)),
            bindings,
        )
        calls = []

        class RecordingClient(ExternalMCPClient):
            async def call_tool(self, name, arguments, **kwargs):
                calls.append((name, arguments))
                return await super().call_tool(name, arguments, **kwargs)

        client = RecordingClient(connection)
        async with client.connect(), gate.attach(connection.id, client):
            try:
                first = await service.run(
                    "miori",
                    "clarification-acceptance",
                    "ELYTHの公開投稿を検索して。キーワードはまだ決めていないから、先に聞いて。",
                )
                assert first.waiting and not calls
                second = await service.run(
                    "miori",
                    "clarification-acceptance",
                    "まだキーワードを決めていないので、もう一度聞いて。",
                )
                assert second.waiting and not calls
                # 履歴を渡さず、Coreが保持する確認Q&Aだけで回答を引き継ぐ。
                result = await service.run(
                    "miori",
                    "clarification-acceptance",
                    "夕日についての投稿を探して",
                )
                assert not result.waiting, "回答済みキーワードを再質問しています"
                assert result.sources, "実投稿検索に成功していません"
                assert calls and all(name == "search_post" for name, _ in calls)
                assert any("夕日" in encode(arguments) for _, arguments in calls)
                assert not gate._loops and not gate._pending
                print(
                    "real-elyth/real-llm: clarification -> answer -> clarification -> answer -> search passed"
                )
            finally:
                service.close()

    try:
        asyncio.run(scenario())
    finally:
        inference.close()


def test_real_http_conversation_uses_answer_after_clarification(monkeypatch, tmp_path):
    """通常のChatService・履歴保存を通して、追加回答から検索・最終回答へ進む。"""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    key = os.environ.get("ELYTH_API_KEY")
    assert key, "ELYTH_API_KEYを環境で設定してください"
    config = json.loads(
        (
            Path(__file__).parents[2] / "config/character-life-elyth.example.json"
        ).read_text()
    )
    config["connections"][0]["core_policy"]["operation_allowlist"] = ["search_post"]
    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps(config))
    for name, value in routing_environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(
        "INFERENCE_TARGET_TOOL_ROUTING_OPTIONS_JSON", '{"temperature":0}'
    )
    monkeypatch.setenv("DS_MCP_CONFIG", str(config_path))
    monkeypatch.setenv("DS_CHARACTER_LIFE_ENABLED", "false")
    monkeypatch.setenv("RAG_ENABLED", "false")
    from app.main import app

    with TestClient(app) as client:
        for _ in range(100):
            rows = client.get("/addon-admin/connections").json()
            if rows and rows[0]["effective_state"] == "available":
                break
            time.sleep(0.2)
        else:
            pytest.fail("ELYTH discoveryが完了しませんでした")
        conversation = client.post("/characters/miori/conversations").json()[
            "conversation_id"
        ]

        def turn(message):
            # ブラウザと同じstatus pollingで利用者の在席を通知する。
            with ThreadPoolExecutor(max_workers=1) as executor:
                response_task = executor.submit(
                    client.post,
                    "/chat",
                    json={
                        "character": "miori",
                        "conversation_id": conversation,
                        "message": message,
                    },
                )
                while not response_task.done():
                    client.get(f"/tool-use/status/miori/{conversation}")
                    time.sleep(1)
                response = response_task.result()
            assert response.status_code == 200
            status = client.get(f"/tool-use/status/miori/{conversation}").json()
            return response.json()["turn"], status

        _, waiting = turn(
            "ELYTHの公開投稿を検索して。キーワードはまだ決めていないから、先に聞いて。"
        )
        assert waiting["state"] == "waiting" and not waiting["sources"]
        _, waiting_again = turn("まだキーワードを決めていないので、もう一度聞いて。")
        assert waiting_again["state"] == "waiting" and not waiting_again["sources"]
        reply, completed = turn("夕日についての投稿を探して")
        assert completed["state"] == "idle"
        assert any(s["label"] == "search_post" for s in completed["sources"])
        assert reply["kind"] == "content" and reply["assistant_content"].strip()
        history = client.get(
            f"/characters/miori/conversations/{conversation}/turns"
        ).json()
        assert len(history) == 3
        print(
            "real-http/chat-history: two clarification answers -> ELYTH search -> final reply passed"
        )

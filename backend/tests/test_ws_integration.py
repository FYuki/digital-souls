import json
import threading
import time
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.memory.chroma_store import MemorySearchResult


def _ollama_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "message": {"role": "assistant", "content": content},
    }
    response.raise_for_status.return_value = None
    return response


def _write_character(tmp_path, character: str, system_prompt: str) -> None:
    character_dir = tmp_path / "characters" / character
    character_dir.mkdir(parents=True)
    character_dir.joinpath("personality.md").write_text(
        system_prompt,
        encoding="utf-8",
    )


def _isolate_memory_paths(tmp_path, monkeypatch) -> None:
    import app.memory.chroma_store as chroma_store
    import app.memory.conversation_log as conversation_log
    import app.memory.rag_service as rag_service

    data_dir = tmp_path / "data"
    monkeypatch.setattr(chroma_store, "DATA_DIR", data_dir)
    monkeypatch.setattr(chroma_store, "CHROMA_PATH", data_dir / "chroma")
    monkeypatch.setattr(conversation_log, "DATA_DIR", data_dir)
    monkeypatch.setattr(conversation_log, "DB_PATH", data_dir / "conversations.db")
    monkeypatch.setattr(rag_service, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        rag_service,
        "FAILED_MEMORY_LOG_PATH",
        data_dir / "failed-memories.jsonl",
    )


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


class TestWebSocketFlowIntegration:
    def test_path_character_prompt_and_message_reach_ollama_payload(
        self, client, tmp_path, monkeypatch
    ):
        import app.characters.loader as loader_module

        characters_dir = tmp_path / "characters" / "miori"
        characters_dir.mkdir(parents=True)
        system_prompt = "# 光織\nあなたは光織です。"
        characters_dir.joinpath("personality.md").write_text(
            system_prompt,
            encoding="utf-8",
        )
        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: tmp_path)

        expected_reply = "光織です。よろしくお願いします。"
        with patch(
            "app.llm.ollama_client.httpx.post",
            return_value=_ollama_response(expected_reply),
        ) as mock_post:
            with client.websocket_connect(
                "/ws/miori?character=ignored&message=ignored",
            ) as websocket:
                websocket.send_json(
                    {"type": "text", "message": "自己紹介してください"},
                )
                response = websocket.receive_json()

        assert response == {"type": "text", "response": expected_reply}

        payload = mock_post.call_args.kwargs["json"]
        assert payload["messages"] == [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "自己紹介してください"},
        ]

    def test_rag_search_injection_and_recording_flow_through_websocket(
        self, tmp_path, monkeypatch
    ):
        import app.characters.loader as loader_module
        import app.memory.rag_service as rag_service

        monkeypatch.setenv("RAG_ENABLED", "true")
        _isolate_memory_paths(tmp_path, monkeypatch)
        system_prompt = "# 光織\nあなたは光織です。"
        _write_character(tmp_path, "miori", system_prompt)
        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: tmp_path)

        stored_memories = []
        llm_payloads = []

        def fake_embed_text(content: str) -> list[float]:
            return [float(len(content))]

        def fake_add_memory(character, record_id, embedding, content, metadata):
            stored_memories.append(
                {
                    "character": character,
                    "record_id": record_id,
                    "embedding": embedding,
                    "content": content,
                    "metadata": metadata,
                }
            )

        def fake_query_memories(character, embedding, n_results):
            return [
                MemorySearchResult(
                    content=memory["content"],
                    timestamp=memory["metadata"]["timestamp"],
                    role=memory["metadata"]["role"],
                )
                for memory in stored_memories[:n_results]
                if memory["character"] == character
            ]

        def capture_post(*args, **kwargs):
            llm_payloads.append(kwargs["json"])
            return _ollama_response("記録しました。")

        monkeypatch.setattr(rag_service, "embed_text", fake_embed_text)
        monkeypatch.setattr(rag_service, "add_memory", fake_add_memory)
        monkeypatch.setattr(rag_service, "query_memories", fake_query_memories)

        with patch("app.llm.ollama_client.httpx.post", side_effect=capture_post):
            with TestClient(app) as client:
                with client.websocket_connect("/ws/miori") as websocket:
                    websocket.send_json(
                        {
                            "type": "text",
                            "message": "農業日誌: 2026-06-23はトマト畑に水やりした",
                        },
                    )
                    first_response = websocket.receive_json()
                    _wait_until(lambda: len(stored_memories) == 1)

                    websocket.send_json(
                        {"type": "text", "message": "前回の畑作業は?"},
                    )
                    second_response = websocket.receive_json()

        assert first_response == {"type": "text", "response": "記録しました。"}
        assert second_response == {"type": "text", "response": "記録しました。"}
        second_system_prompt = llm_payloads[-1]["messages"][0]["content"]
        assert "過去の記憶:" in second_system_prompt
        assert "2026-06-23はトマト畑に水やりした" in second_system_prompt
        assert stored_memories[0]["content"] == (
            "農業日誌: 2026-06-23はトマト畑に水やりした"
        )

    def test_rag_storage_failure_does_not_block_websocket_response_and_writes_fallback(
        self, tmp_path, monkeypatch
    ):
        import app.characters.loader as loader_module
        import app.memory.rag_service as rag_service

        monkeypatch.setenv("RAG_ENABLED", "true")
        _isolate_memory_paths(tmp_path, monkeypatch)
        system_prompt = "# 光織\nあなたは光織です。"
        user_message = "農業日誌: 2026-06-23はナスに追肥した"
        _write_character(tmp_path, "miori", system_prompt)
        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: tmp_path)

        add_started = threading.Event()
        release_add = threading.Event()

        def fake_embed_text(content: str) -> list[float]:
            return [float(len(content))]

        def failing_add_memory(character, record_id, embedding, content, metadata):
            add_started.set()
            release_add.wait(timeout=5)
            raise RuntimeError("injected chroma add failure")

        monkeypatch.setattr(rag_service, "embed_text", fake_embed_text)
        monkeypatch.setattr(rag_service, "query_memories", lambda *args, **kwargs: [])
        monkeypatch.setattr(rag_service, "add_memory", failing_add_memory)

        with patch(
            "app.llm.ollama_client.httpx.post",
            return_value=_ollama_response("農業日誌として保存しました。"),
        ):
            with TestClient(app) as client:
                with client.websocket_connect("/ws/miori") as websocket:
                    release_timer = threading.Timer(1.0, release_add.set)
                    release_timer.start()
                    websocket.send_json({"type": "text", "message": user_message})
                    response = websocket.receive_json()
                    release_timer.cancel()
                    assert add_started.wait(timeout=5)
                    assert not release_add.is_set()
                    assert response == {
                        "type": "text",
                        "response": "農業日誌として保存しました。",
                    }
                    release_add.set()

        failed_path = rag_service.FAILED_MEMORY_LOG_PATH
        _wait_until(lambda: failed_path.exists())
        failed_payloads = [
            json.loads(line)
            for line in failed_path.read_text(encoding="utf-8").splitlines()
        ]
        assert any(
            payload["character"] == "miori"
            and payload["role"] == "user"
            and payload["content"] == user_message
            for payload in failed_payloads
        )

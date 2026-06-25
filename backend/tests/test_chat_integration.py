import json
import time
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app


def _ollama_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "message": {"role": "assistant", "content": content},
    }
    response.raise_for_status.return_value = None
    return response


def _isolate_memory_paths(tmp_path, monkeypatch) -> None:
    import app.memory.conversation_log as conversation_log
    import app.memory.rag_service as rag_service

    data_dir = tmp_path / "data"
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


class TestChatFlowIntegration:
    def test_body_character_prompt_and_message_reach_ollama_payload(
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
            response = client.post(
                "/chat?character=ignored&message=ignored",
                json={"character": "miori", "message": "自己紹介してください"},
            )

        assert response.status_code == 200
        assert response.json() == {"character": "miori", "response": expected_reply}

        payload = mock_post.call_args.kwargs["json"]
        assert payload["messages"] == [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "自己紹介してください"},
        ]

    def test_rag_augmented_prompt_reaches_ollama_and_reply_is_recorded(
        self, tmp_path, monkeypatch
    ):
        import app.characters.loader as loader_module

        monkeypatch.setenv("RAG_ENABLED", "true")
        characters_dir = tmp_path / "characters" / "miori"
        characters_dir.mkdir(parents=True)
        system_prompt = "# 光織\nあなたは光織です。"
        characters_dir.joinpath("personality.md").write_text(
            system_prompt,
            encoding="utf-8",
        )
        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: tmp_path)

        augmented_prompt = f"{system_prompt}\n\n過去の記憶:\n前回は畑の話をした"
        expected_reply = "前回は畑の話をしました。"
        policy = object()
        with patch(
            "app._chat_runtime.resolved_memory_policy",
            return_value=policy,
        ) as mock_policy:
            with patch(
                "app._chat_runtime._rag_service.build_augmented_system_prompt",
                return_value=augmented_prompt,
            ) as mock_build:
                with patch(
                    "app._chat_runtime._rag_service.record_chat_turn"
                ) as mock_record:
                    with patch(
                        "app.llm.ollama_client.httpx.post",
                        return_value=_ollama_response(expected_reply),
                    ) as mock_post:
                        with TestClient(app) as client:
                            response = client.post(
                                "/chat",
                                json={
                                    "character": "miori",
                                    "message": "前回なんの話をしたっけ？",
                                },
                            )

        assert response.status_code == 200
        assert response.json() == {"character": "miori", "response": expected_reply}
        mock_policy.assert_called_once_with()
        mock_build.assert_called_once_with(
            "miori",
            "前回なんの話をしたっけ？",
            system_prompt,
            policy,
        )
        mock_record.assert_called_once()
        assert mock_record.call_args.args[:3] == (
            "miori",
            "前回なんの話をしたっけ？",
            expected_reply,
        )
        assert mock_record.call_args.args[3] is policy
        assert hasattr(mock_record.call_args.args[4], "add_task")

        payload = mock_post.call_args.kwargs["json"]
        assert payload["messages"] == [
            {"role": "system", "content": augmented_prompt},
            {"role": "user", "content": "前回なんの話をしたっけ？"},
        ]

    def test_rag_value_error_falls_back_to_plain_chat_and_writes_failed_memory(
        self, tmp_path, monkeypatch
    ):
        import app.characters.loader as loader_module
        import app.memory.rag_service as rag_service

        monkeypatch.setenv("RAG_ENABLED", "true")
        _isolate_memory_paths(tmp_path, monkeypatch)
        characters_dir = tmp_path / "characters" / "miori"
        characters_dir.mkdir(parents=True)
        system_prompt = "# 光織\nあなたは光織です。"
        characters_dir.joinpath("personality.md").write_text(
            system_prompt,
            encoding="utf-8",
        )
        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: tmp_path)
        monkeypatch.setattr(
            rag_service,
            "embed_text",
            MagicMock(side_effect=ValueError("invalid embedding response")),
        )
        monkeypatch.setattr(rag_service, "query_memories", MagicMock())

        user_message = "農業日誌: 2026-06-23はピーマンに水やりした"
        expected_reply = "農業日誌として保存しました。"
        with patch(
            "app.llm.ollama_client.httpx.post",
            return_value=_ollama_response(expected_reply),
        ) as mock_post:
            with TestClient(app) as client:
                response = client.post(
                    "/chat",
                    json={"character": "miori", "message": user_message},
                )

        assert response.status_code == 200
        assert response.json() == {"character": "miori", "response": expected_reply}
        payload = mock_post.call_args.kwargs["json"]
        assert payload["messages"] == [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        rag_service.query_memories.assert_not_called()

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

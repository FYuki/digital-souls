from unittest.mock import MagicMock, patch


def _ollama_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "message": {"role": "assistant", "content": content},
    }
    response.raise_for_status.return_value = None
    return response


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
        self, client, tmp_path, monkeypatch
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
            "app.chat_service._memory_policy.resolved_memory_policy",
            return_value=policy,
        ) as mock_policy:
            with patch(
                "app.chat_service._rag_service.build_augmented_system_prompt",
                return_value=augmented_prompt,
            ) as mock_build:
                with patch(
                    "app.chat_service._rag_service.record_chat_turn"
                ) as mock_record:
                    with patch(
                        "app.llm.ollama_client.httpx.post",
                        return_value=_ollama_response(expected_reply),
                    ) as mock_post:
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
        assert mock_record.call_args.args[4] is policy

        payload = mock_post.call_args.kwargs["json"]
        assert payload["messages"] == [
            {"role": "system", "content": augmented_prompt},
            {"role": "user", "content": "前回なんの話をしたっけ？"},
        ]

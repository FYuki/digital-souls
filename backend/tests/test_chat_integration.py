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

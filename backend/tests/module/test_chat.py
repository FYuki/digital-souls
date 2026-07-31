import sqlite3
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from app.conversation_history.service import CompletedHistoryExchange
from app.main import app
from app.memory.chroma_store import MemorySearchResult
from app.prompting import CharacterPrompt, PromptInputLimitError
from tests.character_card_test_support import (
    character_card_data,
    character_card_document,
    write_character_card,
)


_LOAD_PERSONALITY = "app._chat_runtime._character_loader.load_character_card"
_GENERATE_RESPONSE = "app._chat_runtime._llm_router.generate_response"
_BUILD_AUGMENTED_SYSTEM_PROMPT = (
    "app._chat_runtime._rag_service.retrieve_prompt_memories"
)
_RECORD_USER_MEMORY_CANDIDATE = (
    "app._chat_runtime._rag_service.record_user_memory_candidate"
)
_RESOLVED_MEMORY_POLICY = "app.main.resolved_memory_policy"
_BUILD_PROMPT = "app._chat_runtime.PromptBuilder.build"
_COMPLETED_EXCHANGES = (
    "app.conversation_history.service.ConversationHistorySession.completed_exchanges"
)

_VALID_BODY = {"character": "miori", "message": "自己紹介してください"}
_PERSONALITY = "# 光織\n穏やかなAIです。"
_LLM_REPLY = "光織です。よろしくお願いします。"


def _character_card(system_prompt: str = _PERSONALITY) -> MagicMock:
    card = MagicMock()
    card.to_character_prompt.return_value = CharacterPrompt(
        description="",
        personality="",
        scenario="",
        system_prompt=system_prompt,
        mes_example="",
        post_history_instructions="",
    )
    return card


def _write_card(tmp_path, system_prompt: str) -> None:
    data = character_card_data(
        description="",
        personality="",
        scenario="",
        system_prompt=system_prompt,
        mes_example="",
        post_history_instructions="",
    )
    write_character_card(
        tmp_path,
        "miori",
        character_card_document(data=data),
    )


def _rag_memory(content: str) -> MemorySearchResult:
    return MemorySearchResult(
        content=content,
        timestamp="2026-07-31T00:00:00+00:00",
        role="user",
    )


def _rag_policy() -> MagicMock:
    from app.memory.memory_policy import resolved_memory_policy

    policy = MagicMock(name="resolved_memory_policy")
    policy.privacy = resolved_memory_policy().privacy
    return policy


def _ollama_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "message": {"role": "assistant", "content": content},
    }
    response.raise_for_status.return_value = None
    return response


class TestChatEndpoint:
    def test_returns_422_with_diagnostics_when_prompt_input_exceeds_limit(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv("RAG_ENABLED", "false")

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _BUILD_PROMPT,
                side_effect=PromptInputLimitError("current_user", 8193, 8192),
            ):
                with TestClient(app, raise_server_exceptions=False) as client:
                    response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 422
        assert response.json() == {
            "detail": (
                "Prompt input exceeds token budget: "
                "region=current_user used=8193 limit=8192"
            )
        }

    def test_prompt_limit_http_response_and_logs_do_not_leak_prompt_bodies(
        self,
        monkeypatch,
        caplog,
    ):
        secrets = {
            "character": "SECRET_HTTP_CHARACTER_37D1",
            "rag": "SECRET_HTTP_RAG_645A",
            "history_user": "SECRET_HTTP_HISTORY_USER_A920",
            "history_assistant": "SECRET_HTTP_HISTORY_ASSISTANT_B781",
            "current_user": "SECRET_HTTP_CURRENT_USER_293A",
        }
        monkeypatch.setenv("RAG_ENABLED", "true")

        def reject_prompt(prompt_input):
            assert prompt_input.character.system_prompt == secrets["character"]
            assert prompt_input.rag.items[0].content.endswith(secrets["rag"])
            assert prompt_input.history.exchanges[0].user_content == secrets[
                "history_user"
            ]
            assert prompt_input.history.exchanges[0].assistant_content == secrets[
                "history_assistant"
            ]
            assert prompt_input.current_user.content == secrets["current_user"]
            raise PromptInputLimitError("current_user", 8193, 8192)

        with patch(
            _LOAD_PERSONALITY,
            return_value=_character_card(secrets["character"]),
        ):
            with patch(
                _BUILD_AUGMENTED_SYSTEM_PROMPT,
                return_value=[_rag_memory(secrets["rag"])],
            ):
                with patch(
                    _COMPLETED_EXCHANGES,
                    return_value=(
                        CompletedHistoryExchange(
                            user_content=secrets["history_user"],
                            assistant_content=secrets["history_assistant"],
                        ),
                    ),
                ):
                    with patch(_BUILD_PROMPT, side_effect=reject_prompt):
                        with TestClient(
                            app,
                            raise_server_exceptions=False,
                        ) as client:
                            response = client.post(
                                "/chat",
                                json={
                                    "character": "miori",
                                    "message": secrets["current_user"],
                                },
                            )

        assert response.status_code == 422
        for secret in secrets.values():
            assert secret not in response.text
            assert secret not in caplog.text

    def test_prompt_limit_persists_failed_conversation_turn(
        self,
        monkeypatch,
        conversation_history_database_path,
    ):
        monkeypatch.setenv("RAG_ENABLED", "false")

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _BUILD_PROMPT,
                side_effect=PromptInputLimitError("current_user", 8193, 8192),
            ):
                with TestClient(app, raise_server_exceptions=False) as client:
                    response = client.post("/chat", json=_VALID_BODY)

        with sqlite3.connect(conversation_history_database_path) as connection:
            statuses = connection.execute(
                "SELECT status FROM conversation_turns ORDER BY created_at"
            ).fetchall()

        assert response.status_code == 422
        assert statuses == [("failed",)]

    def test_returns_200_for_valid_request(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 200

    def test_response_has_character_key(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert "character" in response.json()

    def test_response_has_response_key(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert "response" in response.json()

    def test_response_body_has_exactly_two_keys(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert set(response.json().keys()) == {"character", "response"}

    def test_response_character_echoes_request_character(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert response.json()["character"] == _VALID_BODY["character"]

    def test_response_field_contains_llm_output(self, client):
        expected = "こんにちは、光織です。"
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=expected):
                response = client.post("/chat", json=_VALID_BODY)

        assert response.json()["response"] == expected

    def test_load_personality_called_with_character_name(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()) as mock_load:
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                client.post("/chat", json=_VALID_BODY)

        mock_load.assert_called_once_with("miori")

    def test_generate_response_called_with_personality_and_message(self, client):
        user_message = "農業日誌を記録したい"
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value="了解です") as mock_gen:
                client.post(
                    "/chat",
                    json={"character": "miori", "message": user_message},
                )

        mock_gen.assert_called_once()
        prompt = mock_gen.call_args.args[0]
        contents = [message.content for message in prompt.messages]
        assert _PERSONALITY in contents[0]
        assert user_message == contents[-1]

    def test_generate_response_uses_rag_augmented_system_prompt(self, monkeypatch):
        policy = _rag_policy()
        monkeypatch.setenv("RAG_ENABLED", "true")
        with patch(_RESOLVED_MEMORY_POLICY, return_value=policy):
            with TestClient(app) as client:
                with patch(_LOAD_PERSONALITY, return_value=_character_card()):
                    with patch(
                        _BUILD_AUGMENTED_SYSTEM_PROMPT,
                        return_value=(_rag_memory("前回は畑の話をした"),),
                    ) as mock_build:
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                            with patch(_RECORD_USER_MEMORY_CANDIDATE):
                                client.post("/chat", json=_VALID_BODY)

        mock_build.assert_called_once_with(
            "miori",
            _VALID_BODY["message"],
            policy,
        )
        prompt = mock_gen.call_args.args[0]
        assert "前回は畑の話をした" in prompt.messages[1].content

    def test_records_user_memory_candidate_after_llm_reply(self, monkeypatch):
        policy = _rag_policy()
        monkeypatch.setenv("RAG_ENABLED", "true")
        with patch(_RESOLVED_MEMORY_POLICY, return_value=policy):
            with TestClient(app) as client:
                with patch(_LOAD_PERSONALITY, return_value=_character_card()):
                    with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT, return_value=()):
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                            with patch(
                                _RECORD_USER_MEMORY_CANDIDATE
                            ) as mock_record:
                                response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 200
        mock_record.assert_called_once()
        args, kwargs = mock_record.call_args
        assert args[:2] == ("miori", _VALID_BODY["message"])
        assert args[2] is policy
        assert hasattr(args[3], "add_task")
        assert hasattr(kwargs["privacy_scanner"], "scan")

    def test_rag_disabled_resolves_policy_for_privacy_but_does_not_record(
        self, monkeypatch
    ):
        from app.memory.memory_policy import resolved_memory_policy

        policy = resolved_memory_policy()
        monkeypatch.setenv("RAG_ENABLED", "false")
        with patch(_RESOLVED_MEMORY_POLICY, return_value=policy) as mock_policy:
            with TestClient(app) as client:
                with patch(_LOAD_PERSONALITY, return_value=_character_card()):
                    with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT) as mock_build:
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                            with patch(
                                _RECORD_USER_MEMORY_CANDIDATE
                            ) as mock_record:
                                response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 200
        mock_policy.assert_called_once_with()
        mock_build.assert_not_called()
        mock_record.assert_not_called()

    def test_does_not_record_user_memory_candidate_when_llm_fails(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.HTTPError("boom")):
                with patch(_RECORD_USER_MEMORY_CANDIDATE) as mock_record:
                    response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 502
        mock_record.assert_not_called()

    def test_returns_404_when_character_not_found(self, client):
        with patch(_LOAD_PERSONALITY, side_effect=FileNotFoundError("character not found")):
            response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 404

    def test_does_not_call_llm_when_character_not_found(self, client):
        with patch(_LOAD_PERSONALITY, side_effect=FileNotFoundError("character not found")):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                client.post("/chat", json=_VALID_BODY)

        mock_gen.assert_not_called()

    def test_returns_504_when_llm_request_times_out(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.ReadTimeout("timed out")):
                response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 504

    def test_returns_502_when_llm_request_fails(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.HTTPError("boom")):
                response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 502

    def test_returns_422_when_character_missing(self, client):
        response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 422

    def test_returns_422_when_message_missing(self, client):
        response = client.post("/chat", json={"character": "miori"})

        assert response.status_code == 422

    def test_returns_422_for_empty_body(self, client):
        response = client.post("/chat", json={})

        assert response.status_code == 422

    def test_returns_422_for_empty_message(self, client):
        with patch(_GENERATE_RESPONSE) as generate:
            response = client.post(
                "/chat",
                json={"character": "miori", "message": ""},
            )

        assert response.status_code == 422
        generate.assert_not_called()

    def test_returns_422_for_wrapped_body_envelope(self, client):
        response = client.post(
            "/chat",
            json={"data": {"character": "miori", "message": "hello"}},
        )

        assert response.status_code == 422

    def test_character_comes_from_request_body_not_query(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post(
                    "/chat?character=miori",
                    json={"message": "hello"},
                )

        assert response.status_code == 422

    def test_message_comes_from_request_body_not_query(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post(
                    "/chat?message=hello",
                    json={"character": "miori"},
                )

        assert response.status_code == 422


class TestChatFlow:
    def test_body_character_prompt_and_message_reach_ollama_payload(
        self, client, tmp_path, monkeypatch
    ):
        import app.characters.loader as loader_module

        system_prompt = "# 光織\nあなたは光織です。"
        _write_card(tmp_path, system_prompt)
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
            {"role": "system", "content": f"## 応答方針\n{system_prompt}"},
            {"role": "user", "content": "自己紹介してください"},
        ]

    def test_rag_augmented_prompt_reaches_ollama_and_reply_is_recorded(
        self, tmp_path, monkeypatch
    ):
        import app.characters.loader as loader_module

        monkeypatch.setenv("RAG_ENABLED", "true")
        system_prompt = "# 光織\nあなたは光織です。"
        _write_card(tmp_path, system_prompt)
        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: tmp_path)

        expected_reply = "前回は畑の話をしました。"
        policy = _rag_policy()
        with patch(
            "app.main.resolved_memory_policy",
            return_value=policy,
        ) as mock_policy:
            with patch(
                "app._chat_runtime._rag_service.retrieve_prompt_memories",
                return_value=(_rag_memory("前回は畑の話をした"),),
            ) as mock_build:
                with patch(
                    "app._chat_runtime._rag_service.record_user_memory_candidate"
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
            policy,
        )
        mock_record.assert_called_once()
        assert mock_record.call_args.args[:2] == (
            "miori",
            "前回なんの話をしたっけ？",
        )
        assert mock_record.call_args.args[2] is policy
        assert hasattr(mock_record.call_args.args[3], "add_task")
        assert hasattr(
            mock_record.call_args.kwargs["privacy_scanner"],
            "scan",
        )

        payload = mock_post.call_args.kwargs["json"]
        assert payload["messages"] == [
            {"role": "system", "content": f"## 応答方針\n{system_prompt}"},
            {
                "role": "system",
                "content": "## 関連する記憶\n"
                "[2026-07-31T00:00:00+00:00] (user) 前回は畑の話をした",
            },
            {"role": "user", "content": "前回なんの話をしたっけ？"},
        ]

    def test_rag_value_error_falls_back_without_writing_failed_memory(
        self, tmp_path, monkeypatch
    ):
        import app.characters.loader as loader_module
        import app.memory.rag_service as rag_service

        monkeypatch.setenv("RAG_ENABLED", "true")
        system_prompt = "# 光織\nあなたは光織です。"
        _write_card(tmp_path, system_prompt)
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
            {"role": "system", "content": f"## 応答方針\n{system_prompt}"},
            {"role": "user", "content": user_message},
        ]
        rag_service.query_memories.assert_not_called()

        assert not tmp_path.joinpath("data", "failed-memories.jsonl").exists()

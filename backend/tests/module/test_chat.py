import sqlite3
import sys
from types import ModuleType
from unittest.mock import ANY, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.conversation_history.prompt_history import RestoredHistoryTurn
from app.main import app
from app.memory.chroma_store import MemorySearchResult
from app.prompting import CharacterPrompt, PromptInputLimitError
from app.privacy.semantic.contracts import (
    PrivacyAssessment,
    SemanticAssessmentReasonCode,
    SemanticClassification,
    SemanticPrivacyCategory,
    SubjectScope,
)
from tests.character_card_test_support import (
    character_card_data,
    character_card_document,
    write_character_card,
)
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    OTHER_CONVERSATION_ID,
)


_LOAD_PERSONALITY = "app.main.load_character_card"
_GENERATE_RESPONSE = "app.llm.router.generate_response"
_COUNT_INPUT_TOKENS = "app.llm.router.count_input_tokens"
_BUILD_AUGMENTED_SYSTEM_PROMPT = (
    "app._chat_runtime._rag_service.retrieve_prompt_memories"
)
_RESOLVED_MEMORY_POLICY = "app.main.resolved_memory_policy"
_BUILD_PROMPT = "app.chat_prompt.PromptBuilder.build"
_PROMPT_TURNS = (
    "app.conversation_history.service.ConversationHistorySession.prompt_turns"
)

_VALID_BODY = {
    "character": "miori",
    "conversation_id": str(CONVERSATION_ID),
    "message": "自己紹介してください",
}
_PERSONALITY = "# 光織\n穏やかなAIです。"
_LLM_REPLY = "光織です。よろしくお願いします。"

pytestmark = pytest.mark.usefixtures("existing_chat_conversations")


@pytest.fixture(autouse=True)
def _formal_token_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _COUNT_INPUT_TOKENS, lambda messages, *, settings: len(messages)
    )


def _character_card(system_prompt: str = _PERSONALITY) -> MagicMock:
    card = MagicMock()
    card.data.character_book = None
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
        memory_id="memory-1",
        normalized_text=content,
        occurred_at="2026-07-31T00:00:00.000000Z",
        memory_type="USER_PREFERENCE",
        raw_distance=1.25,
    )


def _rag_outcome(*memories: MemorySearchResult):
    from app.memory.rag_service import RetrievalOutcome

    return RetrievalOutcome(memories, False)


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
            history = tuple(prompt_input.history.newest_first_factory())
            assert history[0].user_content == secrets["history_user"]
            assert history[0].assistant_content == secrets["history_assistant"]
            assert prompt_input.current_user.content == secrets["current_user"]
            raise PromptInputLimitError("current_user", 8193, 8192)

        with patch(
            _LOAD_PERSONALITY,
            return_value=_character_card(secrets["character"]),
        ):
            with patch(
                _BUILD_AUGMENTED_SYSTEM_PROMPT,
                return_value=_rag_outcome(_rag_memory(secrets["rag"])),
            ):
                with patch(
                    _PROMPT_TURNS,
                    return_value=iter(
                        (
                            RestoredHistoryTurn(
                                user_content=secrets["history_user"],
                                assistant_content=secrets["history_assistant"],
                                is_completed=True,
                            ),
                        )
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
                                    "conversation_id": str(CONVERSATION_ID),
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

    def test_response_has_persisted_turn_key(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert "turn" in response.json()

    def test_response_body_has_exactly_two_keys(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert set(response.json().keys()) == {"character", "turn"}

    def test_response_character_echoes_request_character(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert response.json()["character"] == _VALID_BODY["character"]

    def test_persisted_turn_contains_masked_llm_output(self, client):
        expected = "こんにちは、光織です。"
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=expected):
                response = client.post("/chat", json=_VALID_BODY)

        assert response.json()["turn"]["assistant_content"] == expected

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
                    json={
                        "character": "miori",
                        "conversation_id": str(CONVERSATION_ID),
                        "message": user_message,
                    },
                )

        mock_gen.assert_called_once()
        prompt = mock_gen.call_args.args[0]
        contents = [message.content for message in prompt.messages]
        assert _PERSONALITY in contents[0]
        assert user_message == contents[-1]

    def test_generate_response_uses_rag_augmented_system_prompt(
        self, monkeypatch, runtime_paths
    ):
        policy = _rag_policy()
        monkeypatch.setenv("RAG_ENABLED", "true")
        with patch(_RESOLVED_MEMORY_POLICY, return_value=policy):
            with TestClient(app) as client:
                with patch(_LOAD_PERSONALITY, return_value=_character_card()):
                    with patch(
                        _BUILD_AUGMENTED_SYSTEM_PROMPT,
                        return_value=_rag_outcome(_rag_memory("前回は畑の話をした")),
                    ) as mock_build:
                        with patch(
                            _GENERATE_RESPONSE, return_value=_LLM_REPLY
                        ) as mock_gen:
                            client.post("/chat", json=_VALID_BODY)

        mock_build.assert_called_once_with(
            "miori",
            _VALID_BODY["message"],
            policy,
            scanner=ANY,
            classifier=ANY,
            approved_repository=ANY,
            chroma_path=runtime_paths.chroma_path,
            now=ANY,
            timezone="Asia/Tokyo",
        )
        prompt = mock_gen.call_args.args[0]
        assert "前回は畑の話をした" in prompt.messages[1].content

    def test_completed_http_turn_does_not_write_raw_conversation_to_chroma(
        self,
        monkeypatch,
    ):
        from app.memory.memory_policy import resolved_memory_policy

        monkeypatch.setenv("RAG_ENABLED", "true")
        policy = resolved_memory_policy()
        chroma_collection = MagicMock()
        chroma_collection.query.return_value = {
            "ids": [[]],
            "distances": [[]],
        }
        chroma_collection.count.return_value = 0
        chroma_client = MagicMock()
        chroma_client.get_or_create_collection.return_value = chroma_collection
        fake_chromadb = ModuleType("chromadb")
        setattr(
            fake_chromadb,
            "PersistentClient",
            MagicMock(return_value=chroma_client),
        )
        monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
        safe_assessment = PrivacyAssessment(
            classification=SemanticClassification.NOT_SENSITIVE,
            subject_scope=SubjectScope.GENERAL,
            category=SemanticPrivacyCategory.NONE,
            reason_code=SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT,
            classifier_version="test-classifier-v1",
            model_id="test-model",
            model_digest="sha256:test",
            prompt_version="test-prompt-v1",
            policy_version=policy.policy_version,
        )

        with patch(_RESOLVED_MEMORY_POLICY, return_value=policy):
            with patch(
                "app.privacy.semantic.classifier."
                "OllamaSemanticPrivacyClassifier.classify",
                return_value=safe_assessment,
            ):
                with patch("app.memory.rag_service.embed_text", return_value=[0.1]):
                    with patch(
                        "app.memory.chroma_store.upsert_memory_index_entry"
                    ) as upsert_memory_index_entry:
                        with TestClient(app) as client:
                            with patch(
                                _LOAD_PERSONALITY, return_value=_character_card()
                            ):
                                with patch(
                                    _GENERATE_RESPONSE, return_value=_LLM_REPLY
                                ):
                                    response = client.post(
                                        "/chat",
                                        json={
                                            **_VALID_BODY,
                                            "message": "農業日誌: トマト畑に水やりした",
                                        },
                                    )

        assert response.status_code == 200
        assert response.json()["turn"]["assistant_content"] == _LLM_REPLY
        chroma_collection.query.assert_called_once()
        upsert_memory_index_entry.assert_not_called()
        chroma_collection.upsert.assert_not_called()
        chroma_collection.delete.assert_not_called()

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
                            response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 200
        mock_policy.assert_called_once_with()
        mock_build.assert_not_called()

    def test_returns_404_when_character_not_found(self, client):
        with patch(
            _LOAD_PERSONALITY, side_effect=FileNotFoundError("character not found")
        ):
            response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 404

    @pytest.mark.parametrize("operation", ["archive", "hard_delete"])
    def test_returns_safe_404_when_conversation_is_unavailable(
        self,
        client,
        operation: str,
    ):
        repository = client.app.state.conversation_history_repository
        conversation = repository.create_conversation("miori")
        repository.archive_conversation("miori", conversation.conversation_id)
        if operation == "hard_delete":
            repository.hard_delete_conversation(
                "miori",
                conversation.conversation_id,
            )
        payload = {
            **_VALID_BODY,
            "conversation_id": str(conversation.conversation_id),
        }

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                response = client.post("/chat", json=payload)

        assert response.status_code == 404
        assert response.json() == {"detail": "conversation was not found"}
        mock_gen.assert_not_called()

    def test_returns_safe_404_without_creating_an_unknown_conversation(
        self,
        client,
        unknown_chat_conversation,
    ):
        repository = client.app.state.conversation_history_repository

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 404
        assert response.json() == {"detail": "conversation was not found"}
        assert repository.list_active_conversations("miori") == []
        mock_gen.assert_not_called()

    def test_hides_cross_character_conversation_as_the_same_safe_404(
        self,
        client,
    ):
        repository = client.app.state.conversation_history_repository
        conversation = repository.create_conversation("miori")
        payload = {
            **_VALID_BODY,
            "character": "akira",
            "conversation_id": str(conversation.conversation_id),
        }

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                response = client.post("/chat", json=payload)

        assert response.status_code == 404
        assert response.json() == {"detail": "conversation was not found"}
        assert repository.list_active_conversations("akira") == []
        mock_gen.assert_not_called()

    def test_does_not_call_llm_when_character_not_found(self, client):
        with patch(
            _LOAD_PERSONALITY, side_effect=FileNotFoundError("character not found")
        ):
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
                json={
                    "character": "miori",
                    "conversation_id": str(CONVERSATION_ID),
                    "message": "",
                },
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
    def test_rag_disabled_restores_only_same_http_conversation_history(
        self,
        monkeypatch,
        conversation_history_database_path,
    ):
        monkeypatch.setenv("RAG_ENABLED", "false")
        target_user = "password: http-target-user-secret"
        target_assistant = "password: http-target-assistant-secret"
        current_user = "対象会話の前の応答を確認して"
        other_conversation_user = "別会話の内容"
        other_character_user = "別キャラクターの内容"

        def generate(prompt, *, max_output_tokens, settings):
            del max_output_tokens, settings
            current = prompt.messages[-1].content
            if current == target_user:
                return target_assistant
            return "確認しました"

        requests = (
            ("miori", OTHER_CONVERSATION_ID, other_conversation_user),
            ("other", CONVERSATION_ID, other_character_user),
            ("miori", CONVERSATION_ID, target_user),
            ("miori", CONVERSATION_ID, current_user),
        )
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=generate) as mock_generate:
                with TestClient(app) as client:
                    responses = [
                        client.post(
                            "/chat",
                            json={
                                "character": character,
                                "conversation_id": str(conversation_id),
                                "message": message,
                            },
                        )
                        for character, conversation_id, message in requests
                    ]

        assert all(response.status_code == 200 for response in responses)
        prompt = mock_generate.call_args.args[0]
        assert [message.content for message in prompt.messages[-3:]] == [
            "password: [PASSWORD]",
            "password: [PASSWORD]",
            current_user,
        ]
        prompt_contents = [message.content for message in prompt.messages]
        assert other_conversation_user not in prompt_contents
        assert other_character_user not in prompt_contents
        with sqlite3.connect(conversation_history_database_path) as connection:
            stored = connection.execute(
                "SELECT user_content, assistant_content, status "
                "FROM conversation_turns "
                "WHERE character_id = ? AND conversation_id = ? "
                "ORDER BY created_at, turn_id",
                ("miori", str(CONVERSATION_ID)),
            ).fetchall()
        assert stored == [
            ("password: [PASSWORD]", "password: [PASSWORD]", "completed"),
            (current_user, "確認しました", "completed"),
        ]

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
                json=_VALID_BODY,
            )

        assert response.status_code == 200
        assert response.json()["character"] == "miori"
        assert response.json()["turn"]["assistant_content"] == expected_reply

        payload = mock_post.call_args.kwargs["json"]
        assert payload["messages"] == [
            {"role": "system", "content": f"## 応答方針\n{system_prompt}"},
            {"role": "user", "content": "自己紹介してください"},
        ]

    def test_rag_augmented_prompt_reaches_ollama_and_reply_is_recorded(
        self, tmp_path, monkeypatch, runtime_paths
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
                return_value=_rag_outcome(
                    _rag_memory("順位1の記憶"),
                    _rag_memory("順位2の記憶"),
                ),
            ) as mock_build:
                with patch(
                    "app.llm.ollama_client.httpx.post",
                    return_value=_ollama_response(expected_reply),
                ) as mock_post:
                    with TestClient(app) as client:
                        response = client.post(
                            "/chat",
                            json={
                                "character": "miori",
                                "conversation_id": str(CONVERSATION_ID),
                                "message": "前回なんの話をしたっけ？",
                            },
                        )

        assert response.status_code == 200
        assert response.json()["character"] == "miori"
        assert response.json()["turn"]["assistant_content"] == expected_reply
        mock_policy.assert_called_once_with()
        mock_build.assert_called_once_with(
            "miori",
            "前回なんの話をしたっけ？",
            policy,
            scanner=ANY,
            classifier=ANY,
            approved_repository=ANY,
            chroma_path=runtime_paths.chroma_path,
            now=ANY,
            timezone="Asia/Tokyo",
        )
        payload = mock_post.call_args.kwargs["json"]
        assert payload["messages"] == [
            {"role": "system", "content": f"## 応答方針\n{system_prompt}"},
            {
                "role": "system",
                "content": "## 関連する記憶\n"
                "[2026-07-31T09:00:00+09:00] 順位1の記憶",
            },
            {
                "role": "system",
                "content": "## 関連する記憶\n"
                "[2026-07-31T09:00:00+09:00] 順位2の記憶",
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
                    json={
                        "character": "miori",
                        "conversation_id": str(CONVERSATION_ID),
                        "message": user_message,
                    },
                )

        assert response.status_code == 200
        assert response.json()["character"] == "miori"
        assert response.json()["turn"]["assistant_content"] == expected_reply
        payload = mock_post.call_args.kwargs["json"]
        assert payload["messages"] == [
            {"role": "system", "content": f"## 応答方針\n{system_prompt}"},
            {"role": "user", "content": user_message},
        ]
        rag_service.query_memories.assert_not_called()

        assert not tmp_path.joinpath("data", "failed-memories.jsonl").exists()

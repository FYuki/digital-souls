import httpx
from unittest.mock import patch


_LOAD_PERSONALITY = "app.chat_service._character_loader.load_personality"
_GENERATE_RESPONSE = "app.chat_service._llm_router.generate_response"
_BUILD_AUGMENTED_SYSTEM_PROMPT = (
    "app.chat_service._rag_service.build_augmented_system_prompt"
)
_RECORD_CHAT_TURN = "app.chat_service._rag_service.record_chat_turn"
_RESOLVED_MEMORY_POLICY = "app.chat_service._memory_policy.resolved_memory_policy"

_VALID_BODY = {"character": "miori", "message": "自己紹介してください"}
_PERSONALITY = "# 光織\n穏やかなAIです。"
_LLM_REPLY = "光織です。よろしくお願いします。"


class TestChatEndpoint:
    def test_returns_200_for_valid_request(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 200

    def test_response_has_character_key(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert "character" in response.json()

    def test_response_has_response_key(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert "response" in response.json()

    def test_response_body_has_exactly_two_keys(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert set(response.json().keys()) == {"character", "response"}

    def test_response_character_echoes_request_character(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post("/chat", json=_VALID_BODY)

        assert response.json()["character"] == _VALID_BODY["character"]

    def test_response_field_contains_llm_output(self, client):
        expected = "こんにちは、光織です。"
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=expected):
                response = client.post("/chat", json=_VALID_BODY)

        assert response.json()["response"] == expected

    def test_load_personality_called_with_character_name(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY) as mock_load:
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                client.post("/chat", json=_VALID_BODY)

        mock_load.assert_called_once_with("miori")

    def test_generate_response_called_with_personality_and_message(self, client):
        user_message = "農業日誌を記録したい"
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value="了解です") as mock_gen:
                client.post(
                    "/chat",
                    json={"character": "miori", "message": user_message},
                )

        mock_gen.assert_called_once()
        args, kwargs = mock_gen.call_args
        all_args = list(args) + list(kwargs.values())
        assert _PERSONALITY in all_args
        assert user_message in all_args

    def test_generate_response_uses_rag_augmented_system_prompt(self, client):
        policy = object()
        augmented_prompt = f"{_PERSONALITY}\n\n過去の記憶:\n前回は畑の話をした"
        with patch.dict("os.environ", {"RAG_ENABLED": "true"}):
            with patch(_RESOLVED_MEMORY_POLICY, return_value=policy):
                with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
                    with patch(
                        _BUILD_AUGMENTED_SYSTEM_PROMPT,
                        return_value=augmented_prompt,
                    ) as mock_build:
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                            with patch(_RECORD_CHAT_TURN):
                                client.post("/chat", json=_VALID_BODY)

        mock_build.assert_called_once_with(
            "miori",
            _VALID_BODY["message"],
            _PERSONALITY,
            policy,
        )
        mock_gen.assert_called_once_with(augmented_prompt, _VALID_BODY["message"])

    def test_records_chat_turn_after_llm_reply(self, client):
        policy = object()
        with patch.dict("os.environ", {"RAG_ENABLED": "true"}):
            with patch(_RESOLVED_MEMORY_POLICY, return_value=policy):
                with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
                    with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT, return_value=_PERSONALITY):
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                            with patch(_RECORD_CHAT_TURN) as mock_record:
                                response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 200
        mock_record.assert_called_once()
        args, _kwargs = mock_record.call_args
        assert args[:3] == ("miori", _VALID_BODY["message"], _LLM_REPLY)
        assert args[4] is policy

    def test_rag_disabled_does_not_resolve_memory_policy_or_record(self, client):
        with patch.dict("os.environ", {"RAG_ENABLED": "false"}):
            with patch(_RESOLVED_MEMORY_POLICY) as mock_policy:
                with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
                    with patch(_BUILD_AUGMENTED_SYSTEM_PROMPT) as mock_build:
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                            with patch(_RECORD_CHAT_TURN) as mock_record:
                                response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 200
        mock_policy.assert_not_called()
        mock_build.assert_not_called()
        mock_record.assert_not_called()

    def test_does_not_record_chat_turn_when_llm_fails(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.HTTPError("boom")):
                with patch(_RECORD_CHAT_TURN) as mock_record:
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
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, side_effect=httpx.ReadTimeout("timed out")):
                response = client.post("/chat", json=_VALID_BODY)

        assert response.status_code == 504

    def test_returns_502_when_llm_request_fails(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
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

    def test_returns_422_for_wrapped_body_envelope(self, client):
        response = client.post(
            "/chat",
            json={"data": {"character": "miori", "message": "hello"}},
        )

        assert response.status_code == 422

    def test_character_comes_from_request_body_not_query(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post(
                    "/chat?character=miori",
                    json={"message": "hello"},
                )

        assert response.status_code == 422

    def test_message_comes_from_request_body_not_query(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                response = client.post(
                    "/chat?message=hello",
                    json={"character": "miori"},
                )

        assert response.status_code == 422

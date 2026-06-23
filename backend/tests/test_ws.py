import httpx
import pytest
from starlette.websockets import WebSocketDisconnect
from unittest.mock import patch


_LOAD_PERSONALITY = "app.chat_service._character_loader.load_personality"
_GENERATE_RESPONSE = "app.chat_service._llm_router.generate_response"

_PERSONALITY = "# 光織\n穏やかなAIです。"
_LLM_REPLY = "光織です。よろしくお願いします。"


class TestWebSocketEndpoint:
    def test_returns_text_response_for_text_message(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                with client.websocket_connect("/ws/miori") as websocket:
                    websocket.send_json(
                        {"type": "text", "message": "自己紹介してください"},
                    )
                    response = websocket.receive_json()

        assert response == {"type": "text", "response": _LLM_REPLY}

    def test_loads_personality_from_path_character_name(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY) as mock_load:
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                with client.websocket_connect("/ws/miori") as websocket:
                    websocket.send_json(
                        {
                            "type": "text",
                            "character": "ignored",
                            "message": "こんにちは",
                        },
                    )
                    websocket.receive_json()

        mock_load.assert_called_once_with("miori")

    def test_generate_response_uses_loaded_personality_and_root_message(self, client):
        user_message = "農業日誌を記録したい"
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value="了解です") as mock_gen:
                with client.websocket_connect("/ws/miori") as websocket:
                    websocket.send_json(
                        {
                            "type": "text",
                            "data": {"message": "ignored"},
                            "message": user_message,
                        },
                    )
                    websocket.receive_json()

        mock_gen.assert_called_once_with(_PERSONALITY, user_message)

    def test_returns_422_when_payload_is_not_json_object(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect("/ws/miori") as websocket:
                    websocket.send_text('"hello"')
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 422,
            "detail": "WebSocket message must be a JSON object",
        }
        mock_gen.assert_not_called()

    def test_returns_422_when_payload_is_malformed_json(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect("/ws/miori") as websocket:
                    websocket.send_text("{")
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 422,
            "detail": "WebSocket message must be valid JSON",
        }
        mock_gen.assert_not_called()

    def test_returns_422_when_message_type_is_not_text(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect("/ws/miori") as websocket:
                    websocket.send_json({"type": "audio", "message": "こんにちは"})
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 422,
            "detail": "WebSocket message type must be 'text'",
        }
        mock_gen.assert_not_called()

    def test_returns_422_when_text_message_is_not_root_string(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect("/ws/miori") as websocket:
                    websocket.send_json(
                        {
                            "type": "text",
                            "data": {"message": "こんにちは"},
                        },
                    )
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 422,
            "detail": "WebSocket text message must include a string message",
        }
        mock_gen.assert_not_called()

    def test_returns_404_error_and_disconnects_when_character_not_found(self, client):
        with patch(
            _LOAD_PERSONALITY,
            side_effect=FileNotFoundError("character not found"),
        ):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect("/ws/unknown") as websocket:
                    response = websocket.receive_json()
                    with pytest.raises(WebSocketDisconnect):
                        websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 404,
            "detail": "Character 'unknown' not found",
        }
        mock_gen.assert_not_called()

    def test_returns_504_error_when_llm_request_times_out(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(
                _GENERATE_RESPONSE,
                side_effect=httpx.ReadTimeout("timed out"),
            ):
                with client.websocket_connect("/ws/miori") as websocket:
                    websocket.send_json({"type": "text", "message": "こんにちは"})
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 504,
            "detail": "LLM request timed out",
        }

    def test_returns_502_error_when_llm_request_fails(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(
                _GENERATE_RESPONSE,
                side_effect=httpx.HTTPError("boom"),
            ):
                with client.websocket_connect("/ws/miori") as websocket:
                    websocket.send_json({"type": "text", "message": "こんにちは"})
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 502,
            "detail": "LLM request failed",
        }

    def test_connection_continues_after_llm_error(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(
                _GENERATE_RESPONSE,
                side_effect=[httpx.ReadTimeout("timed out"), _LLM_REPLY],
            ):
                with client.websocket_connect("/ws/miori") as websocket:
                    websocket.send_json({"type": "text", "message": "1回目"})
                    first_response = websocket.receive_json()

                    websocket.send_json({"type": "text", "message": "2回目"})
                    second_response = websocket.receive_json()

        assert first_response["status"] == 504
        assert second_response == {"type": "text", "response": _LLM_REPLY}

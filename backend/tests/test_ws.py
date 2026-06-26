import threading

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from unittest.mock import patch

from app.main import app

_LOAD_PERSONALITY = "app._chat_runtime._character_loader.load_personality"
_GENERATE_RESPONSE = "app._chat_runtime._llm_router.generate_response"
_BUILD_AUGMENTED_SYSTEM_PROMPT = (
    "app._chat_runtime._rag_service.build_augmented_system_prompt"
)
_RECORD_CHAT_TURN = "app._chat_runtime._rag_service.record_chat_turn"
_RESOLVED_MEMORY_POLICY = "app._chat_runtime.resolved_memory_policy"
_LOAD_TTS_CONFIG = "app.audio_pipeline.load_tts_config"
_TRANSCRIBE = "app.stt.whisper_client.WhisperTranscriber.transcribe"
_SYNTHESIZE = "app.tts.voicevox_client.VoicevoxClient.synthesize"

_PERSONALITY = "# 光織\n穏やかなAIです。"
_LLM_REPLY = "光織です。よろしくお願いします。"
_PCM_AUDIO = b"\x01\x00\x02\x00"
_ODD_LENGTH_PCM_AUDIO = b"\x01\x00\x03"
_TTS_CONFIG_MISSING_MESSAGE = "'tts_config' field is missing in character card data"


def _tts_config():
    from app.characters.loader import VoicevoxTtsConfig

    return VoicevoxTtsConfig(speaker_id=14)


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

        assert mock_load.call_count == 2
        mock_load.assert_any_call("miori")

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

    def test_rag_enabled_uses_augmented_prompt_and_records_turn(self, monkeypatch):
        user_message = "前回なんの話をしたっけ？"
        policy = object()
        augmented_prompt = f"{_PERSONALITY}\n\n過去の記憶:\n前回は畑の話をした"

        monkeypatch.setenv("RAG_ENABLED", "true")
        with patch(_RESOLVED_MEMORY_POLICY, return_value=policy):
            with TestClient(app) as client:
                with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
                    with patch(
                        _BUILD_AUGMENTED_SYSTEM_PROMPT,
                        return_value=augmented_prompt,
                    ) as mock_build:
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                            with patch(_RECORD_CHAT_TURN) as mock_record:
                                with client.websocket_connect("/ws/miori") as websocket:
                                    websocket.send_json(
                                        {"type": "text", "message": user_message},
                                    )
                                    response = websocket.receive_json()

        assert response == {"type": "text", "response": _LLM_REPLY}
        mock_build.assert_called_once_with(
            "miori",
            user_message,
            _PERSONALITY,
            policy,
        )
        mock_gen.assert_called_once_with(augmented_prompt, user_message)
        mock_record.assert_called_once()
        args, _kwargs = mock_record.call_args
        assert args[:3] == ("miori", user_message, _LLM_REPLY)
        assert args[3] is policy
        assert hasattr(args[4], "add_task")

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

    def test_returns_404_when_character_disappears_after_session_open(self, client):
        with patch(
            _LOAD_PERSONALITY,
            side_effect=[_PERSONALITY, FileNotFoundError("character not found")],
        ):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect("/ws/miori") as websocket:
                    websocket.send_json({"type": "text", "message": "こんにちは"})
                    response = websocket.receive_json()
                    with pytest.raises(WebSocketDisconnect):
                        websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 404,
            "detail": "Character 'miori' not found",
        }
        mock_gen.assert_not_called()

    def test_text_handler_stops_after_server_closes_for_missing_character(self):
        from app.chat_service import CharacterNotFoundError
        from app.routers.ws import _handle_text_frame

        class ClosingWebSocket:
            def __init__(self):
                self.sent_json = []
                self.close_called = False

            async def send_json(self, payload):
                self.sent_json.append(payload)

            async def close(self):
                self.close_called = True

        class MissingCharacterSession:
            def generate_reply(self, message):
                raise CharacterNotFoundError("miori")

        async def run_handler():
            websocket = ClosingWebSocket()
            keep_open = await _handle_text_frame(
                websocket,
                MissingCharacterSession(),
                {"text": '{"type": "text", "message": "こんにちは"}'},
            )
            return keep_open, websocket

        keep_open, websocket = anyio.run(run_handler)

        assert keep_open is False
        assert websocket.close_called is True
        assert websocket.sent_json == [
            {
                "type": "error",
                "status": 404,
                "detail": "Character 'miori' not found",
            },
        ]

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

    def test_returns_wav_bytes_for_binary_audio_frame(self, monkeypatch):
        output_audio = b"RIFF output wav"
        monkeypatch.setenv("VOICEVOX_BASE_URL", "http://voicevox.local:50021")

        with TestClient(app) as client:
            with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
                with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                    with patch(_TRANSCRIBE, return_value="こんにちは") as mock_transcribe:
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                            with patch(_SYNTHESIZE, return_value=output_audio) as mock_tts:
                                with client.websocket_connect("/ws/miori") as websocket:
                                    websocket.send_bytes(_PCM_AUDIO)
                                    response = websocket.receive_bytes()

        assert response == output_audio
        mock_transcribe.assert_called_once_with(_PCM_AUDIO)
        mock_gen.assert_called_once_with(_PERSONALITY, "こんにちは")
        mock_tts.assert_called_once_with(_LLM_REPLY, 14)

    def test_creates_audio_session_in_threadpool(self):
        class StubChatSession:
            def generate_reply(self, message):
                return f"reply:{message}"

        class RecordingChatService:
            def __init__(self):
                self.thread_id = None

            async def create_chat_session(self, character_name):
                self.thread_id = threading.get_ident()
                return StubChatSession()

        class RecordingAudioSession:
            def generate_response_audio(self, audio, reply_generator):
                return b"RIFF output"

        class RecordingAudioService:
            def __init__(self):
                self.thread_id = None

            def create_session(self, character_name):
                self.thread_id = threading.get_ident()
                return RecordingAudioSession()

            def close(self):
                return None

        chat_service = RecordingChatService()
        audio_service = RecordingAudioService()

        with TestClient(app) as client:
            app.state.chat_service = chat_service
            app.state.audio_pipeline_service = audio_service
            with client.websocket_connect("/ws/miori") as websocket:
                websocket.send_bytes(_PCM_AUDIO)
                response = websocket.receive_bytes()

        assert response == b"RIFF output"
        assert chat_service.thread_id is not None
        assert audio_service.thread_id is not None
        assert audio_service.thread_id != chat_service.thread_id

    def test_reuses_audio_pipeline_config_for_connection(self, monkeypatch):
        monkeypatch.setenv("VOICEVOX_BASE_URL", "http://voicevox.local:50021/")

        with TestClient(app) as client:
            with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
                with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()) as mock_config:
                    with patch(
                        _TRANSCRIBE,
                        side_effect=["1つ目の質問", "2つ目の質問"],
                    ):
                        with patch(
                            _GENERATE_RESPONSE,
                            side_effect=["1つ目の応答", "2つ目の応答"],
                        ):
                            with patch(
                                _SYNTHESIZE,
                                side_effect=[b"RIFF first", b"RIFF second"],
                            ) as mock_tts:
                                with client.websocket_connect("/ws/miori") as websocket:
                                    websocket.send_bytes(_PCM_AUDIO)
                                    first_response = websocket.receive_bytes()

                                    monkeypatch.setenv(
                                        "VOICEVOX_BASE_URL",
                                        "http://changed.local:50021",
                                    )
                                    websocket.send_bytes(_PCM_AUDIO)
                                    second_response = websocket.receive_bytes()

        assert first_response == b"RIFF first"
        assert second_response == b"RIFF second"
        mock_config.assert_called_once_with("miori")
        assert [call.args for call in mock_tts.call_args_list] == [
            ("1つ目の応答", 14),
            ("2つ目の応答", 14),
        ]

    def test_returns_502_and_continues_when_stt_transport_fails(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, side_effect=OSError("stt failed")):
                    with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                        with patch(_SYNTHESIZE, return_value=b"RIFF output") as mock_tts:
                            with client.websocket_connect("/ws/miori") as websocket:
                                websocket.send_bytes(_PCM_AUDIO)
                                first_response = websocket.receive_json()

                                websocket.send_json(
                                    {"type": "text", "message": "続けてください"},
                                )
                                second_response = websocket.receive_json()

        assert first_response == {
            "type": "error",
            "status": 502,
            "detail": "STT request failed",
        }
        assert second_response == {"type": "text", "response": _LLM_REPLY}
        mock_gen.assert_called_once_with(_PERSONALITY, "続けてください")
        mock_tts.assert_not_called()

    def test_returns_422_and_continues_when_pcm16_audio_has_odd_byte_length(
        self, client
    ):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, return_value="呼ばれない") as mock_transcribe:
                    with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                        with patch(_SYNTHESIZE, return_value=b"RIFF output") as mock_tts:
                            with client.websocket_connect("/ws/miori") as websocket:
                                websocket.send_bytes(_ODD_LENGTH_PCM_AUDIO)
                                first_response = websocket.receive_json()

                                websocket.send_json(
                                    {"type": "text", "message": "続けてください"},
                                )
                                second_response = websocket.receive_json()

        assert first_response == {
            "type": "error",
            "status": 422,
            "detail": "Audio length must be a multiple of 2 bytes, got 3",
        }
        assert second_response == {"type": "text", "response": _LLM_REPLY}
        mock_gen.assert_called_once_with(_PERSONALITY, "続けてください")
        mock_transcribe.assert_not_called()
        mock_tts.assert_not_called()

    def test_returns_502_and_continues_when_stt_value_error_fails(
        self, client
    ):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, side_effect=ValueError("model rejected audio")):
                    with patch(
                        _GENERATE_RESPONSE,
                        return_value=_LLM_REPLY,
                    ) as mock_gen:
                        with patch(_SYNTHESIZE, return_value=b"RIFF output") as mock_tts:
                            with client.websocket_connect("/ws/miori") as websocket:
                                websocket.send_bytes(_PCM_AUDIO)
                                first_response = websocket.receive_json()

                                websocket.send_json(
                                    {"type": "text", "message": "続けてください"},
                                )
                                second_response = websocket.receive_json()

        assert first_response == {
            "type": "error",
            "status": 502,
            "detail": "STT request failed",
        }
        assert second_response == {"type": "text", "response": _LLM_REPLY}
        mock_gen.assert_called_once_with(_PERSONALITY, "続けてください")
        mock_tts.assert_not_called()

    def test_returns_502_and_continues_when_tts_transport_fails(self, client):
        from app.tts.speech_synthesizer import SpeechSynthesisError

        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, return_value="音声の質問"):
                    with patch(
                        _GENERATE_RESPONSE,
                        side_effect=[_LLM_REPLY, "テキスト応答"],
                    ) as mock_gen:
                        with patch(
                            _SYNTHESIZE,
                            side_effect=SpeechSynthesisError("tts failed"),
                        ):
                            with client.websocket_connect("/ws/miori") as websocket:
                                websocket.send_bytes(_PCM_AUDIO)
                                first_response = websocket.receive_json()

                                websocket.send_json(
                                    {"type": "text", "message": "続けてください"},
                                )
                                second_response = websocket.receive_json()

        assert first_response == {
            "type": "error",
            "status": 502,
            "detail": "VOICEVOX request failed",
        }
        assert second_response == {"type": "text", "response": "テキスト応答"}
        assert [call.args[1] for call in mock_gen.call_args_list] == [
            "音声の質問",
            "続けてください",
        ]

    def test_text_and_binary_frames_share_one_websocket_connection(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, return_value="音声の質問"):
                    with patch(
                        _GENERATE_RESPONSE,
                        side_effect=["テキスト応答", "音声応答"],
                    ) as mock_gen:
                        with patch(_SYNTHESIZE, return_value=b"RIFF voice"):
                            with client.websocket_connect("/ws/miori") as websocket:
                                websocket.send_json(
                                    {"type": "text", "message": "テキストの質問"},
                                )
                                text_response = websocket.receive_json()

                                websocket.send_bytes(_PCM_AUDIO)
                                audio_response = websocket.receive_bytes()

        assert text_response == {"type": "text", "response": "テキスト応答"}
        assert audio_response == b"RIFF voice"
        assert [call.args[1] for call in mock_gen.call_args_list] == [
            "テキストの質問",
            "音声の質問",
        ]

    def test_text_chat_still_works_when_tts_config_is_missing(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(
                _LOAD_TTS_CONFIG,
                side_effect=KeyError(_TTS_CONFIG_MISSING_MESSAGE),
            ):
                with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                    with client.websocket_connect("/ws/miori") as websocket:
                        websocket.send_json({"type": "text", "message": "こんにちは"})
                        response = websocket.receive_json()

        assert response == {"type": "text", "response": _LLM_REPLY}

    def test_returns_500_when_tts_config_is_missing_for_audio_frame(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(
                _LOAD_TTS_CONFIG,
                side_effect=KeyError(_TTS_CONFIG_MISSING_MESSAGE),
            ):
                with patch(_TRANSCRIBE, return_value="こんにちは") as mock_transcribe:
                    with client.websocket_connect("/ws/miori") as websocket:
                        websocket.send_bytes(_PCM_AUDIO)
                        response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 500,
            "detail": _TTS_CONFIG_MISSING_MESSAGE,
        }
        mock_transcribe.assert_not_called()

    def test_returns_500_when_character_card_is_missing_for_audio_frame(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_LOAD_TTS_CONFIG, side_effect=FileNotFoundError("missing card")):
                with patch(_TRANSCRIBE, return_value="こんにちは") as mock_transcribe:
                    with client.websocket_connect("/ws/miori") as websocket:
                        websocket.send_bytes(_PCM_AUDIO)
                        response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 500,
            "detail": "character card is required",
        }
        mock_transcribe.assert_not_called()

    def test_returns_500_when_character_card_is_unreadable_for_audio_frame(
        self, client, caplog
    ):
        card_path = "/tmp/private/miori.card.json"
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(
                _LOAD_TTS_CONFIG,
                side_effect=PermissionError(13, "Permission denied", card_path),
            ):
                with patch(_TRANSCRIBE, return_value="こんにちは") as mock_transcribe:
                    with caplog.at_level("ERROR", logger="app.routers.ws"):
                        with client.websocket_connect("/ws/miori") as websocket:
                            websocket.send_bytes(_PCM_AUDIO)
                            response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 500,
            "detail": "character card is not readable",
        }
        assert card_path not in response["detail"]
        assert all(card_path not in record.getMessage() for record in caplog.records)
        assert all(
            record.exc_info is None
            for record in caplog.records
            if record.name == "app.routers.ws"
        )
        mock_transcribe.assert_not_called()

    def test_returns_500_when_tts_engine_is_invalid_for_audio_frame(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(
                _LOAD_TTS_CONFIG,
                side_effect=ValueError("tts_config.engine must be 'voicevox'"),
            ):
                with patch(_TRANSCRIBE, return_value="こんにちは") as mock_transcribe:
                    with client.websocket_connect("/ws/miori") as websocket:
                        websocket.send_bytes(_PCM_AUDIO)
                        response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 500,
            "detail": "tts_config.engine must be 'voicevox'",
        }
        mock_transcribe.assert_not_called()

    def test_logs_latency_for_audio_pipeline_steps(self, client, caplog):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, return_value="こんにちは"):
                    with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                        with patch(_SYNTHESIZE, return_value=b"RIFF output"):
                            with caplog.at_level("INFO", logger="app.audio_pipeline"):
                                with client.websocket_connect("/ws/miori") as websocket:
                                    websocket.send_bytes(_PCM_AUDIO)
                                    websocket.receive_bytes()

        messages = [record.getMessage() for record in caplog.records]
        assert any("STT completed in" in message for message in messages)
        assert any("LLM completed in" in message for message in messages)
        assert any("VOICEVOX completed in" in message for message in messages)

    def test_logs_websocket_disconnect_code_and_reason(self, caplog):
        from app.routers.ws import websocket_chat

        class StubChatService:
            async def create_chat_session(self, character_name):
                return object()

        class FakeWebSocket:
            def __init__(self):
                self.app = type("App", (), {})()
                self.app.state = type("State", (), {})()
                self.app.state.chat_service = StubChatService()

            async def accept(self):
                return None

            async def receive(self):
                return {
                    "type": "websocket.disconnect",
                    "code": 1001,
                    "reason": "going away",
                }

        with caplog.at_level("INFO", logger="app.routers.ws"):
            anyio.run(websocket_chat, FakeWebSocket(), "miori")

        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "WebSocket disconnected for character 'miori' (code=1001, reason=going away)"
            in message
            for message in messages
        )

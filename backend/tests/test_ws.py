import json
import threading
import time

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from unittest.mock import MagicMock, patch

from app.audio_pipeline import resolve_audio_runtime_config
from app.main import app
from app.memory.chroma_store import MemorySearchResult
from app.tts.voicevox_client import DEFAULT_VOICEVOX_BASE_URL

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


def _wait_for_event(event: threading.Event, label: str, timeout: float = 5.0) -> None:
    if not event.wait(timeout=timeout):
        raise AssertionError(f"{label} was not observed before timeout")


def _tts_config():
    from app.characters.loader import VoicevoxTtsConfig

    return VoicevoxTtsConfig(speaker_id=14)


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


class TestWebSocketEndpoint:
    def test_empty_voicevox_base_url_uses_default_runtime_config(self, monkeypatch):
        monkeypatch.setenv("VOICEVOX_BASE_URL", "")

        runtime_config = resolve_audio_runtime_config()

        assert runtime_config.voicevox_base_url == DEFAULT_VOICEVOX_BASE_URL

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
        import asyncio

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
                asyncio.Lock(),
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

    def test_text_close_path_holds_send_lock_until_close_completes(self):
        import asyncio

        from app.chat_service import CharacterNotFoundError
        from app.routers.ws import _handle_text_frame, _send_json

        class SequencedClosingWebSocket:
            def __init__(self):
                self.events = []
                self.error_sent = anyio.Event()
                self.release_error_send = anyio.Event()

            async def send_json(self, payload):
                self.events.append(("json", payload))
                if payload.get("status") == 404:
                    self.error_sent.set()
                    await self.release_error_send.wait()

            async def close(self):
                await anyio.sleep(0)
                self.events.append(("close", None))

        class MissingCharacterSession:
            def generate_reply(self, message):
                raise CharacterNotFoundError("miori")

        async def run_handler_with_contending_send():
            websocket = SequencedClosingWebSocket()
            send_lock = asyncio.Lock()
            handler_task = asyncio.create_task(
                _handle_text_frame(
                    websocket,
                    send_lock,
                    MissingCharacterSession(),
                    {"text": '{"type": "text", "message": "こんにちは"}'},
                )
            )
            await websocket.error_sent.wait()

            competing_send = asyncio.create_task(
                _send_json(
                    websocket,
                    send_lock,
                    {"type": "text", "response": "competing"},
                )
            )
            await anyio.sleep(0)
            websocket.release_error_send.set()

            keep_open = await handler_task
            await competing_send
            return keep_open, websocket.events

        keep_open, events = anyio.run(run_handler_with_contending_send)

        assert keep_open is False
        assert events == [
            (
                "json",
                {
                    "type": "error",
                    "status": 404,
                    "detail": "Character 'miori' not found",
                },
            ),
            ("close", None),
            ("json", {"type": "text", "response": "competing"}),
        ]

    def test_invalid_audio_frame_allows_following_text_message(self):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with TestClient(app) as client:
                    with client.websocket_connect("/ws/miori") as websocket:
                        websocket.send(
                            {"type": "websocket.receive", "bytes": "not-bytes"}
                        )
                        first_response = websocket.receive_json()

                        websocket.send_json(
                            {"type": "text", "message": "続けてください"}
                        )
                        second_response = websocket.receive_json()

        assert first_response == {
            "type": "error",
            "status": 422,
            "detail": "WebSocket audio frame must be bytes",
        }
        assert second_response == {"type": "text", "response": _LLM_REPLY}
        mock_gen.assert_called_once_with(_PERSONALITY, "続けてください")

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
                                    user_text = websocket.receive_json()
                                    miori_text = websocket.receive_json()
                                    response = websocket.receive_bytes()

        assert user_text == {"type": "text", "speaker": "user", "message": "こんにちは"}
        assert miori_text == {
            "type": "text",
            "speaker": "miori",
            "response": _LLM_REPLY,
        }
        assert response == output_audio
        mock_transcribe.assert_called_once_with(_PCM_AUDIO)
        mock_gen.assert_called_once_with(_PERSONALITY, "こんにちは")
        mock_tts.assert_called_once_with(_LLM_REPLY, 14)

    def test_accepts_audio_frame_below_size_limit(self, monkeypatch):
        from app.routers.ws import MAX_AUDIO_FRAME_BYTES

        output_audio = b"RIFF output wav"
        audio_frame = b"\x01\x00\x02\x00"
        monkeypatch.setenv("VOICEVOX_BASE_URL", "http://voicevox.local:50021")

        assert len(audio_frame) < MAX_AUDIO_FRAME_BYTES

        with TestClient(app) as client:
            with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
                with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                    with patch(_TRANSCRIBE, return_value="こんにちは") as mock_transcribe:
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                            with patch(_SYNTHESIZE, return_value=output_audio):
                                with client.websocket_connect("/ws/miori") as websocket:
                                    websocket.send_bytes(audio_frame)
                                    websocket.receive_json()
                                    websocket.receive_json()
                                    response = websocket.receive_bytes()

        assert response == output_audio
        mock_transcribe.assert_called_once_with(audio_frame)

    def test_accepts_audio_frame_at_exact_size_limit(self, monkeypatch):
        from app.routers.ws import MAX_AUDIO_FRAME_BYTES

        output_audio = b"RIFF exact limit wav"
        audio_frame = b"\x00" * MAX_AUDIO_FRAME_BYTES
        monkeypatch.setenv("VOICEVOX_BASE_URL", "http://voicevox.local:50021")

        with TestClient(app) as client:
            with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
                with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                    with patch(_TRANSCRIBE, return_value="上限ちょうど") as mock_transcribe:
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                            with patch(_SYNTHESIZE, return_value=output_audio):
                                with client.websocket_connect("/ws/miori") as websocket:
                                    websocket.send_bytes(audio_frame)
                                    user_text = websocket.receive_json()
                                    miori_text = websocket.receive_json()
                                    response = websocket.receive_bytes()

        assert user_text == {"type": "text", "speaker": "user", "message": "上限ちょうど"}
        assert miori_text == {
            "type": "text",
            "speaker": "miori",
            "response": _LLM_REPLY,
        }
        assert response == output_audio
        mock_transcribe.assert_called_once_with(audio_frame)

    def test_closes_websocket_when_audio_frame_exceeds_size_limit(
        self, monkeypatch, caplog
    ):
        from app.routers.ws import MAX_AUDIO_FRAME_BYTES

        oversized_audio_frame = b"\x00" * (MAX_AUDIO_FRAME_BYTES + 1)
        monkeypatch.setenv("VOICEVOX_BASE_URL", "http://voicevox.local:50021")

        with TestClient(app) as client:
            with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
                with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()) as mock_load_tts:
                    with patch(_TRANSCRIBE, return_value="呼ばれない") as mock_transcribe:
                        with caplog.at_level("ERROR", logger="app.routers.ws"):
                            with client.websocket_connect("/ws/miori") as websocket:
                                websocket.send_bytes(oversized_audio_frame)
                                with pytest.raises(WebSocketDisconnect) as exc_info:
                                    websocket.receive_json()

        assert exc_info.value.code == 4008
        assert exc_info.value.reason == "Audio frame too large"
        mock_load_tts.assert_not_called()
        mock_transcribe.assert_not_called()
        assert any(
            "Audio frame too large" in record.getMessage()
            for record in caplog.records
        )

    def test_websocket_sends_user_text_miori_text_then_audio(self, monkeypatch):
        output_audio = b"RIFF output"
        monkeypatch.setenv("VOICEVOX_BASE_URL", "http://voicevox.local:50021")

        with TestClient(app) as client:
            with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
                with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                    with patch(_TRANSCRIBE, return_value="音声入力"):
                        with patch(_GENERATE_RESPONSE, return_value="応答:音声入力"):
                            with patch(_SYNTHESIZE, return_value=output_audio):
                                with client.websocket_connect("/ws/miori") as websocket:
                                    websocket.send_bytes(_PCM_AUDIO)
                                    user_text = websocket.receive_json()
                                    miori_text = websocket.receive_json()
                                    response = websocket.receive_bytes()

        assert user_text == {"type": "text", "speaker": "user", "message": "音声入力"}
        assert miori_text == {
            "type": "text",
            "speaker": "miori",
            "response": "応答:音声入力",
        }
        assert response == output_audio

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
                reply = reply_generator("threadpool transcript")
                return "threadpool transcript", reply, b"RIFF output"

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
                websocket.receive_json()
                websocket.receive_json()
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
                                    first_user_text = websocket.receive_json()
                                    first_miori_text = websocket.receive_json()
                                    first_response = websocket.receive_bytes()

                                    monkeypatch.setenv(
                                        "VOICEVOX_BASE_URL",
                                        "http://changed.local:50021",
                                    )
                                    websocket.send_bytes(_PCM_AUDIO)
                                    second_user_text = websocket.receive_json()
                                    second_miori_text = websocket.receive_json()
                                    second_response = websocket.receive_bytes()

        assert first_user_text == {"type": "text", "speaker": "user", "message": "1つ目の質問"}
        assert first_miori_text == {
            "type": "text",
            "speaker": "miori",
            "response": "1つ目の応答",
        }
        assert second_user_text == {"type": "text", "speaker": "user", "message": "2つ目の質問"}
        assert second_miori_text == {
            "type": "text",
            "speaker": "miori",
            "response": "2つ目の応答",
        }
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
                                user_text = websocket.receive_json()
                                miori_text = websocket.receive_json()
                                audio_response = websocket.receive_bytes()

        assert text_response == {"type": "text", "response": "テキスト応答"}
        assert user_text == {"type": "text", "speaker": "user", "message": "音声の質問"}
        assert miori_text == {
            "type": "text",
            "speaker": "miori",
            "response": "音声応答",
        }
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
                                    websocket.receive_json()
                                    websocket.receive_json()
                                    websocket.receive_bytes()

        messages = [record.getMessage() for record in caplog.records]
        assert any("STT completed in" in message for message in messages)
        assert any("LLM completed in" in message for message in messages)
        assert any("VOICEVOX completed in" in message for message in messages)

    def test_enqueue_audio_frame_replaces_pending_frame(self):
        import asyncio

        from app.routers.ws import _enqueue_audio_frame

        async def run_queue_flow():
            queue = asyncio.Queue(maxsize=1)

            _enqueue_audio_frame(queue, b"old")
            _enqueue_audio_frame(queue, b"new")

            return queue.qsize(), await queue.get()

        queue_size, queued_audio = anyio.run(run_queue_flow)

        assert queue_size == 1
        assert queued_audio == b"new"

    def test_audio_response_sends_text_and_bytes_without_interleaving(self):
        import asyncio

        from app.routers.ws import _handle_audio_payload, _send_json

        class StubChatSession:
            def generate_reply(self, message):
                return f"reply:{message}"

        class StubAudioSession:
            def generate_response_audio(self, audio, reply_generator):
                transcript = f"transcript:{audio.decode()}"
                reply = reply_generator(transcript)
                return transcript, reply, b"RIFF " + audio

        class SequencedWebSocket:
            def __init__(self):
                self.sent = []
                self.first_audio_sent = anyio.Event()
                self.release_first_audio = anyio.Event()

            async def send_json(self, payload):
                self.sent.append(("json", payload))
                if payload.get("speaker") == "user":
                    self.first_audio_sent.set()
                    await self.release_first_audio.wait()
                    await anyio.sleep(0)

            async def send_bytes(self, payload):
                self.sent.append(("bytes", payload))

        async def run_flow():
            websocket = SequencedWebSocket()
            send_lock = asyncio.Lock()
            audio_task = asyncio.create_task(
                _handle_audio_payload(
                    websocket,
                    send_lock,
                    StubChatSession(),
                    StubAudioSession(),
                    b"audio",
                )
            )
            await websocket.first_audio_sent.wait()

            text_task = asyncio.create_task(
                _send_json(
                    websocket,
                    send_lock,
                    {"type": "text", "response": "text while audio sends"},
                )
            )
            await anyio.sleep(0)
            websocket.release_first_audio.set()

            keep_open = await audio_task
            await text_task
            return keep_open, websocket.sent

        keep_open, sent = anyio.run(run_flow)

        assert keep_open is True
        assert sent == [
            (
                "json",
                {
                    "type": "text",
                    "speaker": "user",
                    "message": "transcript:audio",
                },
            ),
            (
                "json",
                {
                    "type": "text",
                    "speaker": "miori",
                    "response": "reply:transcript:audio",
                },
            ),
            ("bytes", b"RIFF audio"),
            ("json", {"type": "text", "response": "text while audio sends"}),
        ]

    def test_audio_worker_unexpected_error_sends_500_and_closes(self):
        class StubChatSession:
            def generate_reply(self, message):
                return f"reply:{message}"

        class StubChatService:
            async def create_chat_session(self, character_name):
                return StubChatSession()

        class FailingAudioPipelineService:
            def create_session(self, character_name):
                raise RuntimeError("boom")

            def close(self):
                return None

        with TestClient(app) as client:
            app.state.chat_service = StubChatService()
            app.state.audio_pipeline_service = FailingAudioPipelineService()
            with client.websocket_connect("/ws/miori") as websocket:
                websocket.send_bytes(b"\x01\x00")
                response = websocket.receive_json()
                with pytest.raises(WebSocketDisconnect):
                    websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 500,
            "detail": "Audio processing failed",
        }

    def test_audio_worker_unexpected_error_stops_websocket_chat(self):
        import asyncio

        from app.routers import ws as ws_module

        class StubChatSession:
            def generate_reply(self, message):
                return f"reply:{message}"

        class StubChatService:
            async def create_chat_session(self, character_name):
                return StubChatSession()

        class FailingAudioPipelineService:
            def create_session(self, character_name):
                raise RuntimeError("boom")

        class BlockingReceiveWebSocket:
            def __init__(self):
                self.app = type("App", (), {})()
                self.app.state = type("State", (), {})()
                self.app.state.chat_service = StubChatService()
                self.app.state.audio_pipeline_service = FailingAudioPipelineService()
                self.frames = [{"bytes": b"\x01\x00"}]
                self.accepted = False
                self.closed = False
                self.sent_json = []

            async def accept(self):
                self.accepted = True

            async def receive(self):
                if self.frames:
                    return self.frames.pop(0)
                while True:
                    await anyio.sleep(0)

            async def send_json(self, payload):
                self.sent_json.append(payload)

            async def send_bytes(self, payload):
                return None

            async def close(self, code=1000, reason=None):
                self.closed = True

        async def run_chat():
            websocket = BlockingReceiveWebSocket()
            await asyncio.wait_for(
                ws_module.websocket_chat(websocket, "miori"),
                timeout=0.5,
            )
            return websocket

        websocket = anyio.run(run_chat)

        assert websocket.accepted is True
        assert websocket.sent_json == [
            {
                "type": "error",
                "status": 500,
                "detail": "Audio processing failed",
            }
        ]
        assert websocket.closed is True

    def test_audio_processing_does_not_block_following_text_frame(self):
        class StubChatSession:
            def __init__(self):
                self.messages = []

            def generate_reply(self, message):
                self.messages.append(message)
                return f"reply:{message}"

        class StubChatService:
            async def create_chat_session(self, character_name):
                return StubChatSession()

        class BlockingAudioSession:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()

            def generate_response_audio(self, audio, reply_generator):
                self.started.set()
                self.release.wait(timeout=5)
                reply = reply_generator("audio transcript")
                return "audio transcript", reply, b"RIFF audio"

        class StubAudioPipelineService:
            def __init__(self, session):
                self.session = session

            def create_session(self, character_name):
                return self.session

            def close(self):
                return None

        audio_session = BlockingAudioSession()
        text_response = []
        text_received = threading.Event()

        with TestClient(app) as client:
            app.state.chat_service = StubChatService()
            app.state.audio_pipeline_service = StubAudioPipelineService(audio_session)
            with client.websocket_connect("/ws/miori") as websocket:
                websocket.send_bytes(b"\x01\x00")
                _wait_for_event(audio_session.started, "first audio processing")

                def receive_text_response():
                    text_response.append(websocket.receive_json())
                    text_received.set()

                receiver = threading.Thread(target=receive_text_response)
                receiver.start()
                try:
                    websocket.send_json({"type": "text", "message": "text while audio runs"})
                    _wait_for_event(text_received, "text response")
                finally:
                    audio_session.release.set()
                    receiver.join(timeout=5)

        assert text_response == [{"type": "text", "response": "reply:text while audio runs"}]

    def test_audio_queue_processes_only_latest_pending_frame(self):
        class StubChatSession:
            def generate_reply(self, message):
                return f"reply:{message}"

        class StubChatService:
            async def create_chat_session(self, character_name):
                return StubChatSession()

        class RecordingAudioSession:
            def __init__(self):
                self.calls = []
                self.first_started = threading.Event()
                self.release_first = threading.Event()

            def generate_response_audio(self, audio, reply_generator):
                self.calls.append(audio)
                if audio == b"first":
                    self.first_started.set()
                    self.release_first.wait(timeout=5)
                transcript = f"transcript:{audio.decode()}"
                reply = reply_generator(transcript)
                return transcript, reply, b"RIFF " + audio

        class StubAudioPipelineService:
            def __init__(self, session):
                self.session = session

            def create_session(self, character_name):
                return self.session

            def close(self):
                return None

        audio_session = RecordingAudioSession()
        barrier_responses = []
        barrier_received = threading.Event()

        with TestClient(app) as client:
            app.state.chat_service = StubChatService()
            app.state.audio_pipeline_service = StubAudioPipelineService(audio_session)
            with client.websocket_connect("/ws/miori") as websocket:
                websocket.send_bytes(b"first")
                _wait_for_event(audio_session.first_started, "first audio processing")

                websocket.send_bytes(b"stale")
                websocket.send_bytes(b"latest")

                def receive_barrier_response():
                    barrier_responses.append(websocket.receive_json())
                    barrier_received.set()

                receiver = threading.Thread(target=receive_barrier_response)
                receiver.start()
                websocket.send_json({"type": "text", "message": "barrier"})
                try:
                    _wait_for_event(barrier_received, "barrier text response")
                    audio_session.release_first.set()
                    receiver.join(timeout=5)
                    first_user_text = websocket.receive_json()
                    first_miori_text = websocket.receive_json()
                    first_audio = websocket.receive_bytes()
                    latest_user_text = websocket.receive_json()
                    latest_miori_text = websocket.receive_json()
                    latest_audio = websocket.receive_bytes()
                finally:
                    audio_session.release_first.set()
                    receiver.join(timeout=5)

        assert barrier_responses == [{"type": "text", "response": "reply:barrier"}]
        assert first_user_text == {
            "type": "text",
            "speaker": "user",
            "message": "transcript:first",
        }
        assert first_miori_text == {
            "type": "text",
            "speaker": "miori",
            "response": "reply:transcript:first",
        }
        assert first_audio == b"RIFF first"
        assert latest_user_text == {
            "type": "text",
            "speaker": "user",
            "message": "transcript:latest",
        }
        assert latest_miori_text == {
            "type": "text",
            "speaker": "miori",
            "response": "reply:transcript:latest",
        }
        assert latest_audio == b"RIFF latest"
        assert audio_session.calls == [b"first", b"latest"]

    def test_disconnect_cancels_audio_worker_and_discards_pending_frames(self):
        from app.routers import ws as ws_module

        class StubChatSession:
            def generate_reply(self, message):
                return f"reply:{message}"

        class StubChatService:
            async def create_chat_session(self, character_name):
                return StubChatSession()

        class StubAudioSession:
            def __init__(self):
                self.calls = []

            def generate_response_audio(self, audio, reply_generator):
                self.calls.append(audio)
                return "transcript", reply_generator("transcript"), b"RIFF"

        class StubAudioPipelineService:
            def __init__(self, session):
                self.session = session

            def create_session(self, character_name):
                return self.session

        class FakeWebSocket:
            def __init__(self, audio_session):
                self.app = type("App", (), {})()
                self.app.state = type("State", (), {})()
                self.app.state.chat_service = StubChatService()
                self.app.state.audio_pipeline_service = StubAudioPipelineService(
                    audio_session
                )
                self.frames = [
                    {"bytes": b"stale"},
                    {"bytes": b"latest"},
                    {"type": "websocket.disconnect", "code": 1001, "reason": "bye"},
                ]
                self.accepted = False
                self.sent_json = []
                self.sent_bytes = []

            async def accept(self):
                self.accepted = True

            async def receive(self):
                while not self.frames:
                    await anyio.sleep(0)
                return self.frames.pop(0)

            async def send_json(self, payload):
                self.sent_json.append(payload)

            async def send_bytes(self, payload):
                self.sent_bytes.append(payload)

            async def close(self, code=1000, reason=None):
                return None

        async def run_chat():
            audio_session = StubAudioSession()
            websocket = FakeWebSocket(audio_session)
            await ws_module.websocket_chat(websocket, "miori")
            await anyio.sleep(0)
            return websocket, audio_session

        websocket, audio_session = anyio.run(run_chat)

        assert websocket.accepted is True
        assert websocket.sent_json == []
        assert websocket.sent_bytes == []
        assert audio_session.calls == []

    def test_disconnect_does_not_wait_for_inflight_audio_worker(self):
        import asyncio

        from app.routers import ws as ws_module

        class StubChatSession:
            def generate_reply(self, message):
                return f"reply:{message}"

        class StubChatService:
            async def create_chat_session(self, character_name):
                return StubChatSession()

        class BlockingAudioSession:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()

            def generate_response_audio(self, audio, reply_generator):
                self.started.set()
                self.release.wait(timeout=5)
                return "transcript", reply_generator("transcript"), b"RIFF"

        class StubAudioPipelineService:
            def __init__(self, session):
                self.session = session

            def create_session(self, character_name):
                return self.session

        audio_session = BlockingAudioSession()

        class DisconnectingWebSocket:
            def __init__(self, audio_session):
                self.app = type("App", (), {})()
                self.app.state = type("State", (), {})()
                self.app.state.chat_service = StubChatService()
                self.app.state.audio_pipeline_service = StubAudioPipelineService(
                    audio_session
                )
                self.audio_session = audio_session
                self.frames = [{"bytes": b"first"}]

            async def accept(self):
                return None

            async def receive(self):
                if self.frames:
                    return self.frames.pop(0)
                while not self.audio_session.started.is_set():
                    await anyio.sleep(0)
                return {
                    "type": "websocket.disconnect",
                    "code": 1001,
                    "reason": "bye",
                }

            async def send_json(self, payload):
                return None

            async def send_bytes(self, payload):
                return None

            async def close(self, code=1000, reason=None):
                return None

        async def run_chat():
            websocket = DisconnectingWebSocket(audio_session)
            try:
                await asyncio.wait_for(
                    ws_module.websocket_chat(websocket, "miori"),
                    timeout=0.5,
                )
            finally:
                audio_session.release.set()

        anyio.run(run_chat)

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

    def test_receive_frame_preserves_disconnect_code_and_reason(self):
        from app.routers.ws import _receive_frame

        class DisconnectingWebSocket:
            async def receive(self):
                return {
                    "type": "websocket.disconnect",
                    "code": 1001,
                    "reason": "going away",
                }

        with pytest.raises(WebSocketDisconnect) as exc_info:
            anyio.run(_receive_frame, DisconnectingWebSocket())

        assert exc_info.value.code == 1001
        assert exc_info.value.reason == "going away"

    def test_audio_worker_normal_exit_stops_websocket_chat(self):
        import asyncio

        from app.chat_service import CharacterNotFoundError
        from app.routers import ws as ws_module

        class StubChatSession:
            def generate_reply(self, message):
                raise CharacterNotFoundError("miori")

        class StubChatService:
            async def create_chat_session(self, character_name):
                return StubChatSession()

        class StubAudioSession:
            def generate_response_audio(self, audio, reply_generator):
                transcript = "transcript"
                reply = reply_generator(transcript)
                return transcript, reply, b"RIFF"

        class StubAudioPipelineService:
            def create_session(self, character_name):
                return StubAudioSession()

        class BlockingReceiveWebSocket:
            def __init__(self):
                self.app = type("App", (), {})()
                self.app.state = type("State", (), {})()
                self.app.state.chat_service = StubChatService()
                self.app.state.audio_pipeline_service = StubAudioPipelineService()
                self.frames = [{"bytes": b"\x01\x00"}]
                self.accepted = False
                self.closed = False
                self.sent_json = []

            async def accept(self):
                self.accepted = True

            async def receive(self):
                if self.frames:
                    return self.frames.pop(0)
                while True:
                    await anyio.sleep(0)

            async def send_json(self, payload):
                self.sent_json.append(payload)

            async def send_bytes(self, payload):
                return None

            async def close(self, code=1000, reason=None):
                self.closed = True

        async def run_chat():
            websocket = BlockingReceiveWebSocket()
            await asyncio.wait_for(
                ws_module.websocket_chat(websocket, "miori"),
                timeout=0.5,
            )
            return websocket

        websocket = anyio.run(run_chat)

        assert websocket.accepted is True
        assert websocket.sent_json == [
            {
                "type": "error",
                "status": 404,
                "detail": "Character 'miori' not found",
            }
        ]
        assert websocket.closed is True

    def test_disconnect_discards_pending_frame_while_first_is_inflight(self):
        import asyncio

        from app.routers import ws as ws_module

        class StubChatSession:
            def generate_reply(self, message):
                return f"reply:{message}"

        class StubChatService:
            async def create_chat_session(self, character_name):
                return StubChatSession()

        class BlockingAudioSession:
            def __init__(self):
                self.calls = []
                self.started = threading.Event()
                self.release = threading.Event()

            def generate_response_audio(self, audio, reply_generator):
                self.calls.append(audio)
                if audio == b"first":
                    self.started.set()
                    self.release.wait(timeout=5)
                transcript = f"transcript:{audio.decode()}"
                reply = reply_generator(transcript)
                return transcript, reply, b"RIFF " + audio

        class StubAudioPipelineService:
            def __init__(self, session):
                self.session = session

            def create_session(self, character_name):
                return self.session

        audio_session = BlockingAudioSession()

        class DisconnectingWebSocket:
            def __init__(self):
                self.app = type("App", (), {})()
                self.app.state = type("State", (), {})()
                self.app.state.chat_service = StubChatService()
                self.app.state.audio_pipeline_service = StubAudioPipelineService(
                    audio_session
                )
                self.frames = [{"bytes": b"first"}]
                self.pending_sent = False

            async def accept(self):
                return None

            async def receive(self):
                if self.frames:
                    return self.frames.pop(0)
                while not audio_session.started.is_set():
                    await anyio.sleep(0)
                if not self.pending_sent:
                    self.pending_sent = True
                    return {"bytes": b"latest"}
                return {
                    "type": "websocket.disconnect",
                    "code": 1001,
                    "reason": "bye",
                }

            async def send_json(self, payload):
                return None

            async def send_bytes(self, payload):
                return None

            async def close(self, code=1000, reason=None):
                return None

        async def run_chat():
            websocket = DisconnectingWebSocket()
            try:
                await asyncio.wait_for(
                    ws_module.websocket_chat(websocket, "miori"),
                    timeout=2.0,
                )
            finally:
                audio_session.release.set()

        anyio.run(run_chat)

        assert b"latest" not in audio_session.calls

    def test_audio_session_failure_does_not_close_connection(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_PERSONALITY):
            with patch(
                _LOAD_TTS_CONFIG,
                side_effect=KeyError(_TTS_CONFIG_MISSING_MESSAGE),
            ):
                with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                    with client.websocket_connect("/ws/miori") as websocket:
                        websocket.send_bytes(_PCM_AUDIO)
                        response_500 = websocket.receive_json()

                        websocket.send_json({"type": "text", "message": "接続確認"})
                        response_text = websocket.receive_json()

        assert response_500 == {
            "type": "error",
            "status": 500,
            "detail": _TTS_CONFIG_MISSING_MESSAGE,
        }
        assert response_text == {"type": "text", "response": _LLM_REPLY}


class TestWebSocketFlow:
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

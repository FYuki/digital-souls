import sqlite3
import threading
import time

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from unittest.mock import ANY, MagicMock, patch

from app.audio_pipeline import resolve_audio_runtime_config
from app.inference.contracts import ProviderTextResult
from app.main import app
from app.memory.chroma_store import MemorySearchResult
from app.memory.rag_service import RetrievalOutcome
from app.prompting import CharacterPrompt, PromptInputLimitError
from app.prompting.builder import PromptBuilder
from tests.chat_reply_test_support import persisted_reply
from tests.character_card_test_support import (
    character_card_data,
    character_card_document,
    write_character_card,
)
from tests.conversation_history_test_support import (
    CONVERSATION_ID,
    OTHER_CONVERSATION_ID,
    TURN_ID,
)

from tests.ws_test_support import (
    _LOAD_PERSONALITY,
    _GENERATE_RESPONSE,
    _COUNT_INPUT_TOKENS,
    _BUILD_AUGMENTED_SYSTEM_PROMPT,
    _RESOLVED_MEMORY_POLICY,
    _LOAD_TTS_CONFIG,
    _TRANSCRIBE,
    _SYNTHESIZE,
    _BUILD_PROMPT,
    _PERSONALITY,
    _LLM_REPLY,
    _formal_token_counter,
    _PCM_AUDIO,
    _ODD_LENGTH_PCM_AUDIO,
    _TTS_CONFIG_MISSING_MESSAGE,
    _WS_URL,
    pytestmark,
    _StubDeliverySession,
    _character_card,
    _generated_contents,
    _generated_user_messages,
    _wait_for_event,
    _assert_persisted_content_frame,
    _persisted_content_frame,
    _tts_config,
    _ollama_response,
    _write_character,
    _wait_until,
)

class TestWebSocketAudioEndpoint:
    def test_returns_wav_bytes_for_binary_audio_frame(self, monkeypatch):
        output_audio = b"RIFF output wav"
        monkeypatch.setenv("VOICEVOX_BASE_URL", "http://voicevox.local:50021")

        with TestClient(app) as client:
            with patch(_LOAD_PERSONALITY, return_value=_character_card()):
                with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                    with patch(_TRANSCRIBE, return_value="こんにちは") as mock_transcribe:
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                            with patch(_SYNTHESIZE, return_value=output_audio) as mock_tts:
                                with client.websocket_connect(_WS_URL) as websocket:
                                    websocket.send_bytes(_PCM_AUDIO)
                                    persisted_turn = websocket.receive_json()
                                    response = websocket.receive_bytes()

        _assert_persisted_content_frame(persisted_turn, _LLM_REPLY)
        assert response == output_audio
        mock_transcribe.assert_called_once_with(_PCM_AUDIO)
        assert _generated_contents(mock_gen)[-1] == "こんにちは"
        mock_tts.assert_called_once_with(_LLM_REPLY, 14)


    def test_accepts_audio_frame_below_size_limit(self, monkeypatch):
        from app.routers.ws import MAX_AUDIO_FRAME_BYTES

        output_audio = b"RIFF output wav"
        audio_frame = b"\x01\x00\x02\x00"
        monkeypatch.setenv("VOICEVOX_BASE_URL", "http://voicevox.local:50021")

        assert len(audio_frame) < MAX_AUDIO_FRAME_BYTES

        with TestClient(app) as client:
            with patch(_LOAD_PERSONALITY, return_value=_character_card()):
                with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                    with patch(_TRANSCRIBE, return_value="こんにちは") as mock_transcribe:
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                            with patch(_SYNTHESIZE, return_value=output_audio):
                                with client.websocket_connect(_WS_URL) as websocket:
                                    websocket.send_bytes(audio_frame)
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
            with patch(_LOAD_PERSONALITY, return_value=_character_card()):
                with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                    with patch(_TRANSCRIBE, return_value="上限ちょうど") as mock_transcribe:
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                            with patch(_SYNTHESIZE, return_value=output_audio):
                                with client.websocket_connect(_WS_URL) as websocket:
                                    websocket.send_bytes(audio_frame)
                                    persisted_turn = websocket.receive_json()
                                    response = websocket.receive_bytes()

        _assert_persisted_content_frame(persisted_turn, _LLM_REPLY)
        assert response == output_audio
        mock_transcribe.assert_called_once_with(audio_frame)


    def test_closes_websocket_when_audio_frame_exceeds_size_limit(
        self, monkeypatch, caplog
    ):
        from app.routers.ws import MAX_AUDIO_FRAME_BYTES

        oversized_audio_frame = b"\x00" * (MAX_AUDIO_FRAME_BYTES + 1)
        monkeypatch.setenv("VOICEVOX_BASE_URL", "http://voicevox.local:50021")

        with TestClient(app) as client:
            with patch(_LOAD_PERSONALITY, return_value=_character_card()):
                with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()) as mock_load_tts:
                    with patch(_TRANSCRIBE, return_value="呼ばれない") as mock_transcribe:
                        with caplog.at_level("ERROR", logger="app.routers.ws"):
                            with client.websocket_connect(_WS_URL) as websocket:
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
            with patch(_LOAD_PERSONALITY, return_value=_character_card()):
                with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                    with patch(_TRANSCRIBE, return_value="音声入力"):
                        with patch(_GENERATE_RESPONSE, return_value="応答:音声入力"):
                            with patch(_SYNTHESIZE, return_value=output_audio):
                                with client.websocket_connect(_WS_URL) as websocket:
                                    websocket.send_bytes(_PCM_AUDIO)
                                    persisted_turn = websocket.receive_json()
                                    response = websocket.receive_bytes()

        _assert_persisted_content_frame(persisted_turn, "応答:音声入力")
        assert response == output_audio


    def test_creates_audio_session_in_threadpool(self):
        class StubChatSession(_StubDeliverySession):
            def generate_reply(self, message):
                return persisted_reply(f"reply:{message}", TURN_ID)

        class RecordingChatService:
            def __init__(self):
                self.thread_id = None

            async def create_chat_session(self, character_name, conversation_id):
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
            with client.websocket_connect(_WS_URL) as websocket:
                websocket.send_bytes(_PCM_AUDIO)
                websocket.receive_json()
                response = websocket.receive_bytes()

        assert response == b"RIFF output"
        assert chat_service.thread_id is not None
        assert audio_service.thread_id is not None
        assert audio_service.thread_id != chat_service.thread_id


    def test_reuses_audio_pipeline_config_for_connection(self, monkeypatch):
        monkeypatch.setenv("VOICEVOX_BASE_URL", "http://voicevox.local:50021/")

        with TestClient(app) as client:
            with patch(_LOAD_PERSONALITY, return_value=_character_card()):
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
                                with client.websocket_connect(_WS_URL) as websocket:
                                    websocket.send_bytes(_PCM_AUDIO)
                                    first_turn = websocket.receive_json()
                                    first_response = websocket.receive_bytes()

                                    monkeypatch.setenv(
                                        "VOICEVOX_BASE_URL",
                                        "http://changed.local:50021",
                                    )
                                    websocket.send_bytes(_PCM_AUDIO)
                                    second_turn = websocket.receive_json()
                                    second_response = websocket.receive_bytes()

        _assert_persisted_content_frame(first_turn, "1つ目の応答")
        _assert_persisted_content_frame(second_turn, "2つ目の応答")
        assert first_response == b"RIFF first"
        assert second_response == b"RIFF second"
        mock_config.assert_called_once_with("miori")
        assert [call.args for call in mock_tts.call_args_list] == [
            ("1つ目の応答", 14),
            ("2つ目の応答", 14),
        ]


    def test_returns_502_and_continues_when_stt_transport_fails(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, side_effect=OSError("stt failed")):
                    with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                        with patch(_SYNTHESIZE, return_value=b"RIFF output") as mock_tts:
                            with client.websocket_connect(_WS_URL) as websocket:
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
        _assert_persisted_content_frame(second_response, _LLM_REPLY)
        assert _generated_contents(mock_gen)[-1] == "続けてください"
        mock_tts.assert_not_called()


    def test_returns_422_and_continues_when_pcm16_audio_has_odd_byte_length(
        self, client
    ):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, return_value="呼ばれない") as mock_transcribe:
                    with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                        with patch(_SYNTHESIZE, return_value=b"RIFF output") as mock_tts:
                            with client.websocket_connect(_WS_URL) as websocket:
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
        _assert_persisted_content_frame(second_response, _LLM_REPLY)
        assert _generated_contents(mock_gen)[-1] == "続けてください"
        mock_transcribe.assert_not_called()
        mock_tts.assert_not_called()


    def test_returns_502_and_continues_when_stt_value_error_fails(
        self, client
    ):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, side_effect=ValueError("model rejected audio")):
                    with patch(
                        _GENERATE_RESPONSE,
                        return_value=_LLM_REPLY,
                    ) as mock_gen:
                        with patch(_SYNTHESIZE, return_value=b"RIFF output") as mock_tts:
                            with client.websocket_connect(_WS_URL) as websocket:
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
        _assert_persisted_content_frame(second_response, _LLM_REPLY)
        assert _generated_contents(mock_gen)[-1] == "続けてください"
        mock_tts.assert_not_called()


    def test_returns_502_and_continues_when_tts_transport_fails(self, client):
        from app.tts.speech_synthesizer import SpeechSynthesisError

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
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
                            with client.websocket_connect(_WS_URL) as websocket:
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
        _assert_persisted_content_frame(second_response, "テキスト応答")
        assert _generated_user_messages(mock_gen) == [
            "音声の質問",
            "続けてください",
        ]


    def test_text_and_binary_frames_share_one_websocket_connection(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, return_value="音声の質問"):
                    with patch(
                        _GENERATE_RESPONSE,
                        side_effect=["テキスト応答", "音声応答"],
                    ) as mock_gen:
                        with patch(_SYNTHESIZE, return_value=b"RIFF voice"):
                            with client.websocket_connect(_WS_URL) as websocket:
                                websocket.send_json(
                                    {"type": "text", "message": "テキストの質問"},
                                )
                                text_response = websocket.receive_json()

                                websocket.send_bytes(_PCM_AUDIO)
                                audio_turn = websocket.receive_json()
                                audio_response = websocket.receive_bytes()

        _assert_persisted_content_frame(text_response, "テキスト応答")
        _assert_persisted_content_frame(audio_turn, "音声応答")
        assert audio_response == b"RIFF voice"
        assert _generated_user_messages(mock_gen) == [
            "テキストの質問",
            "音声の質問",
        ]


    def test_text_chat_still_works_when_tts_config_is_missing(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _LOAD_TTS_CONFIG,
                side_effect=KeyError(_TTS_CONFIG_MISSING_MESSAGE),
            ):
                with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                    with client.websocket_connect(_WS_URL) as websocket:
                        websocket.send_json({"type": "text", "message": "こんにちは"})
                        response = websocket.receive_json()

        _assert_persisted_content_frame(response, _LLM_REPLY)


    def test_returns_500_when_tts_config_is_missing_for_audio_frame(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _LOAD_TTS_CONFIG,
                side_effect=KeyError(_TTS_CONFIG_MISSING_MESSAGE),
            ):
                with patch(_TRANSCRIBE, return_value="こんにちは") as mock_transcribe:
                    with client.websocket_connect(_WS_URL) as websocket:
                        websocket.send_bytes(_PCM_AUDIO)
                        response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 500,
            "detail": _TTS_CONFIG_MISSING_MESSAGE,
        }
        mock_transcribe.assert_not_called()


    def test_returns_500_when_character_card_is_missing_for_audio_frame(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_LOAD_TTS_CONFIG, side_effect=FileNotFoundError("missing card")):
                with patch(_TRANSCRIBE, return_value="こんにちは") as mock_transcribe:
                    with client.websocket_connect(_WS_URL) as websocket:
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
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _LOAD_TTS_CONFIG,
                side_effect=PermissionError(13, "Permission denied", card_path),
            ):
                with patch(_TRANSCRIBE, return_value="こんにちは") as mock_transcribe:
                    with caplog.at_level("ERROR", logger="app.routers.ws"):
                        with client.websocket_connect(_WS_URL) as websocket:
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
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _LOAD_TTS_CONFIG,
                side_effect=ValueError("tts_config.engine must be 'voicevox'"),
            ):
                with patch(_TRANSCRIBE, return_value="こんにちは") as mock_transcribe:
                    with client.websocket_connect(_WS_URL) as websocket:
                        websocket.send_bytes(_PCM_AUDIO)
                        response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 500,
            "detail": "tts_config.engine must be 'voicevox'",
        }
        mock_transcribe.assert_not_called()


    def test_logs_latency_for_audio_pipeline_steps(self, client, caplog):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, return_value="こんにちは"):
                    with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                        with patch(_SYNTHESIZE, return_value=b"RIFF output"):
                            with caplog.at_level("INFO", logger="app.audio_pipeline"):
                                with client.websocket_connect(_WS_URL) as websocket:
                                    websocket.send_bytes(_PCM_AUDIO)
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

            queue_size = queue.qsize()
            queued_audio = await queue.get()
            queue.task_done()
            await asyncio.wait_for(queue.join(), timeout=0.1)
            return queue_size, queued_audio

        queue_size, queued_audio = anyio.run(run_queue_flow)

        assert queue_size == 1
        assert queued_audio == b"new"


    @pytest.mark.parametrize(
        ("error_name", "expected_keep_open", "expected_closed"),
        [
            pytest.param("character_not_found", False, True, id="character-not-found"),
            pytest.param("timeout", True, False, id="timeout"),
            pytest.param("backend", True, False, id="backend"),
        ],
    )
    def test_should_use_shared_mapping_for_audio_chat_errors(
        self,
        monkeypatch,
        error_name,
        expected_keep_open,
        expected_closed,
    ):
        import asyncio

        from app.chat_service import (
            CharacterNotFoundError,
            ChatBackendError,
            ChatTimeoutError,
        )
        from app.routers import ws as ws_module

        errors = {
            "character_not_found": CharacterNotFoundError("miori"),
            "timeout": ChatTimeoutError(),
            "backend": ChatBackendError(),
        }
        error = errors[error_name]
        mapped_error = (590, "mapped audio error")
        map_chat_error = MagicMock(return_value=mapped_error)
        monkeypatch.setattr(ws_module, "_map_chat_error", map_chat_error)

        class StubChatSession(_StubDeliverySession):
            def generate_reply(self, message):
                return persisted_reply(f"reply:{message}", TURN_ID)

        class FailingAudioSession:
            def generate_response_audio(self, audio, reply_generator):
                raise error

        class RecordingWebSocket:
            def __init__(self):
                self.sent_json = []
                self.closed = False

            async def send_json(self, payload):
                self.sent_json.append(payload)

            async def close(self):
                self.closed = True

        async def run_handler():
            websocket = RecordingWebSocket()
            keep_open = await ws_module._handle_audio_payload(
                websocket,
                asyncio.Lock(),
                StubChatSession(),
                FailingAudioSession(),
                b"audio",
                "miori",
                ws_module._ConnectionMeasurementState(),
            )
            return keep_open, websocket

        keep_open, websocket = anyio.run(run_handler)

        mapped_status, mapped_detail = mapped_error
        map_chat_error.assert_called_once_with(error)
        assert keep_open is expected_keep_open
        assert websocket.sent_json == [
            {"type": "error", "status": mapped_status, "detail": mapped_detail}
        ]
        assert websocket.closed is expected_closed


    def test_audio_response_sends_text_and_bytes_without_interleaving(self):
        import asyncio

        from app.routers.ws import (
            _ConnectionMeasurementState,
            _handle_audio_payload,
            _send_json,
        )

        class StubChatSession(_StubDeliverySession):
            def generate_reply(self, message):
                return persisted_reply(f"reply:{message}", TURN_ID)

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
                if "turn" in payload:
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
                    "miori",
                    _ConnectionMeasurementState(),
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
            ("json", _persisted_content_frame("reply:transcript:audio")),
            ("bytes", b"RIFF audio"),
            ("json", {"type": "text", "response": "text while audio sends"}),
        ]


    def test_audio_worker_unexpected_error_sends_500_and_closes(self):
        class StubChatSession(_StubDeliverySession):
            def generate_reply(self, message):
                return persisted_reply(f"reply:{message}", TURN_ID)

        class StubChatService:
            async def create_chat_session(self, character_name, conversation_id):
                return StubChatSession()

        class FailingAudioPipelineService:
            def create_session(self, character_name):
                raise RuntimeError("boom")

            def close(self):
                return None

        with TestClient(app) as client:
            app.state.chat_service = StubChatService()
            app.state.audio_pipeline_service = FailingAudioPipelineService()
            with client.websocket_connect(_WS_URL) as websocket:
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

        class StubChatSession(_StubDeliverySession):
            def generate_reply(self, message):
                return persisted_reply(f"reply:{message}", TURN_ID)

        class StubChatService:
            async def create_chat_session(self, character_name, conversation_id):
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
                ws_module.websocket_chat(websocket, "miori", CONVERSATION_ID),
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
        class StubChatSession(_StubDeliverySession):
            def __init__(self):
                self.messages = []

            def generate_reply(self, message):
                self.messages.append(message)
                return persisted_reply(f"reply:{message}", TURN_ID)

        class StubChatService:
            async def create_chat_session(self, character_name, conversation_id):
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
            with client.websocket_connect(_WS_URL) as websocket:
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

        assert text_response == [
            _persisted_content_frame("reply:text while audio runs")
        ]


    def test_audio_queue_processes_only_latest_pending_frame(self):
        class StubChatSession(_StubDeliverySession):
            def generate_reply(self, message):
                return persisted_reply(f"reply:{message}", TURN_ID)

        class StubChatService:
            async def create_chat_session(self, character_name, conversation_id):
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
            with client.websocket_connect(_WS_URL) as websocket:
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
                    first_turn = websocket.receive_json()
                    first_audio = websocket.receive_bytes()
                    latest_turn = websocket.receive_json()
                    latest_audio = websocket.receive_bytes()
                finally:
                    audio_session.release_first.set()
                    receiver.join(timeout=5)

        assert len(barrier_responses) == 1
        _assert_persisted_content_frame(barrier_responses[0], "reply:barrier")
        _assert_persisted_content_frame(first_turn, "reply:transcript:first")
        assert first_audio == b"RIFF first"
        _assert_persisted_content_frame(latest_turn, "reply:transcript:latest")
        assert latest_audio == b"RIFF latest"
        assert audio_session.calls == [b"first", b"latest"]


    def test_disconnect_cancels_audio_worker_and_discards_pending_frames(self):
        from app.routers import ws as ws_module

        class StubChatSession(_StubDeliverySession):
            def generate_reply(self, message):
                return persisted_reply(f"reply:{message}", TURN_ID)

        class StubChatService:
            async def create_chat_session(self, character_name, conversation_id):
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
            await ws_module.websocket_chat(websocket, "miori", CONVERSATION_ID)
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

        class StubChatSession(_StubDeliverySession):
            def generate_reply(self, message):
                return persisted_reply(f"reply:{message}", TURN_ID)

        class StubChatService:
            async def create_chat_session(self, character_name, conversation_id):
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
                    ws_module.websocket_chat(websocket, "miori", CONVERSATION_ID),
                    timeout=0.5,
                )
            finally:
                audio_session.release.set()

        anyio.run(run_chat)


    def test_logs_websocket_disconnect_code_without_client_reason(self, caplog):
        from app.routers.ws import websocket_chat

        raw_secret = "password: websocket-disconnect-secret"

        class StubChatService:
            async def create_chat_session(self, character_name, conversation_id):
                return _StubDeliverySession()

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
                    "reason": raw_secret,
                }

        with caplog.at_level("INFO", logger="app.routers.ws"):
            anyio.run(websocket_chat, FakeWebSocket(), "miori", CONVERSATION_ID)

        messages = [record.getMessage() for record in caplog.records]
        assert "WebSocket disconnected for character 'miori' (code=1001)" in messages
        assert raw_secret not in caplog.text


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

        class StubChatSession(_StubDeliverySession):
            def generate_reply(self, message):
                raise CharacterNotFoundError("miori")

        class StubChatService:
            async def create_chat_session(self, character_name, conversation_id):
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
                ws_module.websocket_chat(websocket, "miori", CONVERSATION_ID),
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

        class StubChatSession(_StubDeliverySession):
            def generate_reply(self, message):
                return persisted_reply(f"reply:{message}", TURN_ID)

        class StubChatService:
            async def create_chat_session(self, character_name, conversation_id):
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
                    ws_module.websocket_chat(websocket, "miori", CONVERSATION_ID),
                    timeout=2.0,
                )
            finally:
                audio_session.release.set()

        anyio.run(run_chat)

        assert b"latest" not in audio_session.calls


    def test_audio_session_failure_does_not_close_connection(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _LOAD_TTS_CONFIG,
                side_effect=KeyError(_TTS_CONFIG_MISSING_MESSAGE),
            ):
                with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                    with client.websocket_connect(_WS_URL) as websocket:
                        websocket.send_bytes(_PCM_AUDIO)
                        response_500 = websocket.receive_json()

                        websocket.send_json({"type": "text", "message": "接続確認"})
                        response_text = websocket.receive_json()

        assert response_500 == {
            "type": "error",
            "status": 500,
            "detail": _TTS_CONFIG_MISSING_MESSAGE,
        }
        _assert_persisted_content_frame(response_text, _LLM_REPLY)

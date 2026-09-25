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

class TestWebSocketEndpoint:
    def test_correlates_audio_metadata_with_the_following_binary_response(
        self,
        client,
    ):
        session_id = "01992f57-8c65-79d0-924f-e2cd79bc01cd"
        utterance_id = "01992f57-8c65-79d0-924f-e2cd79bc02de"
        event_id = "01992f57-8c65-79d0-924f-e2cd79bc03ef"
        url = f"{_WS_URL}&session_id={session_id}"

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, return_value="こんにちは"):
                    with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                        with patch(_SYNTHESIZE, return_value=b"RIFF output"):
                            with client.websocket_connect(url) as websocket:
                                websocket.send_json(
                                    {
                                        "type": "audio_metadata",
                                        "event_id": event_id,
                                        "utterance_id": utterance_id,
                                    }
                                )
                                websocket.send_bytes(_PCM_AUDIO)
                                persisted_turn = websocket.receive_json()
                                audio_metadata = websocket.receive_json()
                                response_audio = websocket.receive_bytes()
                                websocket.send_json(
                                    {
                                        "type": "measurement_event",
                                        "event_id": event_id,
                                        "session_id": session_id,
                                        "utterance_id": utterance_id,
                                        "response_id": audio_metadata["response_id"],
                                        "name": "first_playback",
                                        "timestamp": 1234.5,
                                        "clock_domain": "client_monotonic",
                                        "unit": "millisecond",
                                    }
                                )
                                websocket.send_json(
                                    {"type": "text", "message": "continue"}
                                )
                                continued_turn = websocket.receive_json()

        _assert_persisted_content_frame(persisted_turn, _LLM_REPLY)
        assert audio_metadata["type"] == "audio_response_metadata"
        assert audio_metadata["session_id"] == session_id
        assert audio_metadata["utterance_id"] == utterance_id
        assert isinstance(audio_metadata["response_id"], str)
        assert audio_metadata["response_id"]
        assert response_audio == b"RIFF output"
        _assert_persisted_content_frame(continued_turn, _LLM_REPLY)


    def test_records_measurements_while_audio_send_is_still_in_progress(
        self,
        client,
    ):
        session_id = "01992f57-8c65-79d0-924f-e2cd79bc01cd"
        utterance_id = "01992f57-8c65-79d0-924f-e2cd79bc02de"
        request_event_id = "01992f57-8c65-79d0-924f-e2cd79bc0301"
        received_event_id = "01992f57-8c65-79d0-924f-e2cd79bc0302"
        playback_event_id = "01992f57-8c65-79d0-924f-e2cd79bc0303"
        audio_published = threading.Event()
        release_audio_send = threading.Event()
        received_recorded = threading.Event()
        playback_recorded = threading.Event()
        recorder = MagicMock()
        url = f"{_WS_URL}&session_id={session_id}"

        async def block_after_audio_publish(websocket, payload):
            await websocket.send_bytes(payload)
            audio_published.set()
            await anyio.to_thread.run_sync(release_audio_send.wait)

        def record_client_event(_websocket, event):
            recorder.record(event)
            if event.name == "client_audio_received":
                received_recorded.set()
            elif event.name == "first_playback":
                playback_recorded.set()

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, return_value="こんにちは"):
                    with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                        with patch(_SYNTHESIZE, return_value=b"RIFF output"):
                            with patch(
                                "app.routers.ws._send_bytes_unlocked",
                                side_effect=block_after_audio_publish,
                            ):
                                with patch(
                                    "app.routers.ws._record_trace_event",
                                    side_effect=record_client_event,
                                ):
                                    with client.websocket_connect(url) as websocket:
                                        websocket.send_json({
                                            "type": "audio_metadata",
                                            "event_id": request_event_id,
                                            "utterance_id": utterance_id,
                                        })
                                        websocket.send_bytes(_PCM_AUDIO)
                                        persisted_turn = websocket.receive_json()
                                        metadata = websocket.receive_json()
                                        response_audio = websocket.receive_bytes()
                                        _wait_for_event(audio_published, "audio publish")

                                        def measurement(event_id, name):
                                            return {
                                                "type": "measurement_event",
                                                "event_id": event_id,
                                                "session_id": session_id,
                                                "utterance_id": utterance_id,
                                                "response_id": metadata["response_id"],
                                                "name": name,
                                                "timestamp": 1234.5,
                                                "clock_domain": "client_monotonic",
                                                "unit": "millisecond",
                                            }

                                        try:
                                            websocket.send_json(measurement(
                                                received_event_id,
                                                "client_audio_received",
                                            ))
                                            websocket.send_json(measurement(
                                                playback_event_id,
                                                "first_playback",
                                            ))
                                            _wait_for_event(
                                                received_recorded,
                                                "client audio receipt measurement",
                                            )
                                            _wait_for_event(
                                                playback_recorded,
                                                "first playback measurement",
                                            )
                                        finally:
                                            release_audio_send.set()

                                        websocket.send_json(
                                            {"type": "text", "message": "continue"}
                                        )
                                        continued_turn = websocket.receive_json()

        _assert_persisted_content_frame(persisted_turn, _LLM_REPLY)
        assert metadata["type"] == "audio_response_metadata"
        assert response_audio == b"RIFF output"
        _assert_persisted_content_frame(continued_turn, _LLM_REPLY)
        client_events = [
            call.args[0]
            for call in recorder.record.call_args_list
            if call.args[0].name in {"client_audio_received", "first_playback"}
        ]
        assert [event.name for event in client_events] == [
            "client_audio_received",
            "first_playback",
        ]
        assert all(event.response_id == metadata["response_id"] for event in client_events)


    def test_revoking_response_preserves_other_issued_responses(self):
        from app.routers.ws import (
            AudioCorrelation,
            WebSocketMessageError,
            _ConnectionMeasurementState,
        )
        from app.voice_metrics import TraceEvent

        state = _ConnectionMeasurementState()
        revoked_response_id = "01992f57-8c65-79d0-924f-e2cd79bc0401"
        retained_response_id = "01992f57-8c65-79d0-924f-e2cd79bc0402"
        revoked_correlation = AudioCorrelation(
            event_id="01992f57-8c65-79d0-924f-e2cd79bc0301",
            session_id="01992f57-8c65-79d0-924f-e2cd79bc01cd",
            utterance_id="01992f57-8c65-79d0-924f-e2cd79bc0201",
            measurement=None,
        )
        retained_correlation = AudioCorrelation(
            event_id="01992f57-8c65-79d0-924f-e2cd79bc0302",
            session_id="01992f57-8c65-79d0-924f-e2cd79bc01cd",
            utterance_id="01992f57-8c65-79d0-924f-e2cd79bc0202",
            measurement=None,
        )

        def measurement(event_id, response_id, correlation):
            return TraceEvent(
                schema_version="1.0",
                measurement_kind="automated_test",
                event_id=event_id,
                character_id="miori",
                session_id=correlation.session_id,
                utterance_id=correlation.utterance_id,
                response_id=response_id,
                name="client_audio_received",
                stage="transport",
                outcome="success",
                timestamp=1234.5,
                clock_domain="client_monotonic",
                unit="millisecond",
            )

        state.issue(revoked_response_id, revoked_correlation)
        state.issue(retained_response_id, retained_correlation)
        state.revoke(revoked_response_id)

        with pytest.raises(
            WebSocketMessageError,
            match="measurement response_id was not issued",
        ):
            state.consume(measurement(
                "01992f57-8c65-79d0-924f-e2cd79bc0501",
                revoked_response_id,
                revoked_correlation,
            ))
        state.consume(measurement(
            "01992f57-8c65-79d0-924f-e2cd79bc0502",
            retained_response_id,
            retained_correlation,
        ))


    @pytest.mark.parametrize(
        "failure_stage",
        [
            pytest.param("metadata", id="metadata"),
            pytest.param("audio", id="audio"),
        ],
    )
    def test_revokes_measurement_response_when_audio_send_fails(
        self,
        failure_stage,
    ):
        import asyncio

        from app.routers.ws import (
            AudioCorrelation,
            WebSocketMessageError,
            _ConnectionMeasurementState,
            _handle_audio_payload,
        )
        from app.voice_metrics import TraceEvent

        correlation = AudioCorrelation(
            event_id="01992f57-8c65-79d0-924f-e2cd79bc0301",
            session_id="01992f57-8c65-79d0-924f-e2cd79bc01cd",
            utterance_id="01992f57-8c65-79d0-924f-e2cd79bc02de",
            measurement=None,
        )
        state = _ConnectionMeasurementState()

        class RecordingChatSession(_StubDeliverySession):
            def __init__(self):
                self.failed = []

            def generate_reply(self, message):
                return persisted_reply(f"reply:{message}", TURN_ID)

            def mark_delivery_failed(self, turn_id):
                self.failed.append(turn_id)

        class StubAudioSession:
            def generate_response_audio(
                self,
                audio,
                reply_generator,
                *,
                measurement,
            ):
                del measurement
                reply = reply_generator(f"transcript:{audio.decode()}")
                return "transcript", reply, b"RIFF " + audio

        class FailingWebSocket:
            def __init__(self):
                self.app = type("App", (), {})()
                self.app.state = type("State", (), {})()
                self.app.state.voice_measurement_kind = "automated_test"
                self.response_id = None

            async def send_json(self, payload):
                if payload.get("type") == "audio_response_metadata":
                    self.response_id = payload["response_id"]
                    if failure_stage == "metadata":
                        raise RuntimeError("synthetic metadata send failure")

            async def send_bytes(self, payload):
                assert payload == b"RIFF audio"
                if failure_stage == "audio":
                    raise RuntimeError("synthetic audio send failure")

        async def run_handler():
            websocket = FailingWebSocket()
            chat_session = RecordingChatSession()
            with pytest.raises(
                RuntimeError,
                match=f"synthetic {failure_stage} send failure",
            ):
                await _handle_audio_payload(
                    websocket,
                    asyncio.Lock(),
                    chat_session,
                    StubAudioSession(),
                    b"audio",
                    "miori",
                    state,
                    correlation,
                    1,
                )
            return websocket, chat_session

        websocket, chat_session = anyio.run(run_handler)
        assert websocket.response_id is not None
        failed_response_event = TraceEvent(
            schema_version="1.0",
            measurement_kind="automated_test",
            event_id="01992f57-8c65-79d0-924f-e2cd79bc0501",
            character_id="miori",
            session_id=correlation.session_id,
            utterance_id=correlation.utterance_id,
            response_id=websocket.response_id,
            name="client_audio_received",
            stage="transport",
            outcome="success",
            timestamp=1234.5,
            clock_domain="client_monotonic",
            unit="millisecond",
        )
        with pytest.raises(
            WebSocketMessageError,
            match="measurement response_id was not issued",
        ):
            state.consume(failed_response_event)
        assert chat_session.failed == [TURN_ID]


    def test_records_only_unused_measurements_for_responses_issued_by_connection(
        self,
        client,
    ):
        session_id = "01992f57-8c65-79d0-924f-e2cd79bc01cd"
        utterance_id = "01992f57-8c65-79d0-924f-e2cd79bc02de"
        request_event_id = "01992f57-8c65-79d0-924f-e2cd79bc0301"
        received_event_id = "01992f57-8c65-79d0-924f-e2cd79bc0302"
        playback_event_id = "01992f57-8c65-79d0-924f-e2cd79bc0303"
        recorder = MagicMock()
        record_event = patch(
            "app.routers.ws._record_trace_event",
            side_effect=lambda _websocket, event: recorder.record(event),
        )
        url = _WS_URL

        try:
            record_event.start()
            with patch(_LOAD_PERSONALITY, return_value=_character_card()):
                with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                    with patch(_TRANSCRIBE, return_value="こんにちは"):
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                            with patch(_SYNTHESIZE, return_value=b"RIFF output"):
                                with client.websocket_connect(url) as websocket:
                                    websocket.send_json({
                                        "type": "audio_metadata",
                                        "event_id": request_event_id,
                                        "session_id": session_id,
                                        "utterance_id": utterance_id,
                                    })
                                    websocket.send_bytes(_PCM_AUDIO)
                                    websocket.receive_json()
                                    metadata = websocket.receive_json()
                                    websocket.receive_bytes()

                                    def measurement(event_id, name, **overrides):
                                        payload = {
                                            "type": "measurement_event",
                                            "event_id": event_id,
                                            "session_id": session_id,
                                            "utterance_id": utterance_id,
                                            "response_id": metadata["response_id"],
                                            "name": name,
                                            "timestamp": 1234.5,
                                            "clock_domain": "client_monotonic",
                                            "unit": "millisecond",
                                        }
                                        payload.update(overrides)
                                        return payload

                                    recorded_before_measurements = (
                                        recorder.record.call_count
                                    )
                                    websocket.send_json(measurement(
                                        "01992f57-8c65-79d0-924f-e2cd79bc0304",
                                        "client_audio_received",
                                        session_id=(
                                            "01992f57-8c65-79d0-924f-e2cd79bc0777"
                                        ),
                                    ))
                                    assert websocket.receive_json()["status"] == 422
                                    assert (
                                        recorder.record.call_count
                                        == recorded_before_measurements
                                    )

                                    websocket.send_json(measurement(
                                        received_event_id,
                                        "client_audio_received",
                                    ))
                                    websocket.send_json(measurement(
                                        received_event_id,
                                        "first_playback",
                                    ))
                                    assert websocket.receive_json()["status"] == 422
                                    assert (
                                        recorder.record.call_count
                                        == recorded_before_measurements + 1
                                    )

                                    websocket.send_json(measurement(
                                        playback_event_id,
                                        "first_playback",
                                    ))
                                    websocket.send_json(measurement(
                                        "01992f57-8c65-79d0-924f-e2cd79bc0305",
                                        "first_playback",
                                    ))
                                    assert websocket.receive_json()["status"] == 422
                                    assert (
                                        recorder.record.call_count
                                        == recorded_before_measurements + 2
                                    )
                                    websocket.send_json(measurement(
                                        "01992f57-8c65-79d0-924f-e2cd79bc0306",
                                        "client_audio_received",
                                        response_id="01992f57-8c65-79d0-924f-e2cd79bc0999",
                                    ))
                                    assert websocket.receive_json()["status"] == 422
                                    assert (
                                        recorder.record.call_count
                                        == recorded_before_measurements + 2
                                    )
                                    websocket.send_json(measurement(
                                        "01992f57-8c65-79d0-924f-e2cd79bc0307",
                                        "client_audio_received",
                                        utterance_id="01992f57-8c65-79d0-924f-e2cd79bc0888",
                                    ))
                                    assert websocket.receive_json()["status"] == 422
                                    assert (
                                        recorder.record.call_count
                                        == recorded_before_measurements + 2
                                    )
                                    websocket.send_json({
                                        "type": "text",
                                        "message": "continue",
                                    })
                                    continued_turn = websocket.receive_json()
        finally:
            record_event.stop()

        client_events = [
            call.args[0]
            for call in recorder.record.call_args_list
            if call.args[0].name in {"client_audio_received", "first_playback"}
        ]
        assert [
            (event.name, event.event_id, event.response_id)
            for event in client_events
        ] == [
            (
                "client_audio_received",
                received_event_id,
                metadata["response_id"],
            ),
            (
                "first_playback",
                playback_event_id,
                metadata["response_id"],
            ),
        ]
        _assert_persisted_content_frame(continued_turn, _LLM_REPLY)


    def test_returns_422_and_continues_after_text_prompt_limit(
        self,
        client,
        caplog,
    ):
        secret = "SECRET_WS_TEXT_CURRENT_USER_C934"
        original_build = PromptBuilder.build
        calls = 0

        def fail_first_build(builder, prompt_input):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PromptInputLimitError("current_user", 8193, 8192)
            return original_build(builder, prompt_input)

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_BUILD_PROMPT, autospec=True, side_effect=fail_first_build):
                with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                    with client.websocket_connect(_WS_URL) as websocket:
                        websocket.send_json({"type": "text", "message": secret})
                        limit_response = websocket.receive_json()

                        websocket.send_json(
                            {"type": "text", "message": "continue after limit"}
                        )
                        normal_response = websocket.receive_json()

        assert limit_response == {
            "type": "error",
            "status": 422,
            "detail": (
                "Prompt input exceeds token budget: "
                "region=current_user used=8193 limit=8192"
            ),
        }
        _assert_persisted_content_frame(normal_response, _LLM_REPLY)
        assert secret not in str(limit_response)
        assert secret not in caplog.text


    def test_returns_422_and_continues_after_audio_prompt_limit(
        self,
        client,
        caplog,
    ):
        secret = "SECRET_WS_AUDIO_TRANSCRIPT_8B51"
        original_build = PromptBuilder.build
        calls = 0

        def fail_first_build(builder, prompt_input):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PromptInputLimitError("current_user", 8193, 8192)
            return original_build(builder, prompt_input)

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, return_value=secret):
                    with patch(
                        _BUILD_PROMPT,
                        autospec=True,
                        side_effect=fail_first_build,
                    ):
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                            with patch(_SYNTHESIZE) as mock_tts:
                                with client.websocket_connect(
                                    _WS_URL
                                ) as websocket:
                                    websocket.send_bytes(_PCM_AUDIO)
                                    limit_response = websocket.receive_json()

                                    websocket.send_json(
                                        {
                                            "type": "text",
                                            "message": "continue after audio limit",
                                        }
                                    )
                                    normal_response = websocket.receive_json()

        assert limit_response == {
            "type": "error",
            "status": 422,
            "detail": (
                "Prompt input exceeds token budget: "
                "region=current_user used=8193 limit=8192"
            ),
        }
        _assert_persisted_content_frame(normal_response, _LLM_REPLY)
        assert secret not in str(limit_response)
        assert secret not in caplog.text
        mock_tts.assert_not_called()


    @pytest.mark.parametrize(
        ("error", "expected_status", "expected_detail"),
        [
            pytest.param(
                "character_not_found",
                404,
                "Character 'miori' not found",
                id="character-not-found",
            ),
            pytest.param(
                "timeout",
                504,
                "LLM request timed out",
                id="timeout",
            ),
            pytest.param(
                "backend",
                502,
                "LLM request failed",
                id="backend",
            ),
        ],
    )
    def test_should_map_chat_error_to_websocket_status_and_detail(
        self,
        error,
        expected_status,
        expected_detail,
    ):
        from app.chat_service import (
            CharacterNotFoundError,
            ChatBackendError,
            ChatTimeoutError,
        )
        from app.routers.ws import _map_chat_error

        errors = {
            "character_not_found": CharacterNotFoundError("miori"),
            "timeout": ChatTimeoutError(),
            "backend": ChatBackendError(),
        }

        result = _map_chat_error(errors[error])

        assert result == (expected_status, expected_detail)


    def test_unset_voicevox_base_url_uses_canonical_runtime_config(
        self, monkeypatch, runtime_paths
    ):
        monkeypatch.delenv("VOICEVOX_BASE_URL", raising=False)

        from app.model_settings import resolve_model_settings

        runtime_config = resolve_audio_runtime_config(
            resolve_model_settings({}), runtime_paths
        )

        assert runtime_config.voicevox_base_url == "http://127.0.0.1:50021"


    def test_empty_voicevox_base_url_uses_canonical_runtime_config(
        self, monkeypatch, runtime_paths
    ):
        monkeypatch.setenv("VOICEVOX_BASE_URL", "")

        from app.model_settings import resolve_model_settings

        runtime_config = resolve_audio_runtime_config(
            resolve_model_settings({}), runtime_paths
        )

        assert runtime_config.voicevox_base_url == "http://127.0.0.1:50021"


    def test_explicit_voicevox_base_url_removes_trailing_slash(
        self, monkeypatch, runtime_paths
    ):
        monkeypatch.setenv("VOICEVOX_BASE_URL", "http://voicevox.local:50021/")

        from app.model_settings import resolve_model_settings

        runtime_config = resolve_audio_runtime_config(
            resolve_model_settings({}), runtime_paths
        )

        assert runtime_config.voicevox_base_url == "http://voicevox.local:50021"


    def test_returns_text_response_for_text_message(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                with client.websocket_connect(_WS_URL) as websocket:
                    websocket.send_json(
                        {"type": "text", "message": "自己紹介してください"},
                    )
                    response = websocket.receive_json()

        _assert_persisted_content_frame(response, _LLM_REPLY)


    def test_loads_personality_from_path_character_name(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()) as mock_load:
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY):
                with client.websocket_connect(_WS_URL) as websocket:
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
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value="了解です") as mock_gen:
                with client.websocket_connect(_WS_URL) as websocket:
                    websocket.send_json(
                        {
                            "type": "text",
                            "data": {"message": "ignored"},
                            "message": user_message,
                        },
                    )
                    websocket.receive_json()

        assert _generated_contents(mock_gen)[-1] == user_message


    def test_rag_enabled_uses_retrieved_memories_in_the_prompt(
        self, monkeypatch, runtime_paths
    ):
        from tests.module.test_chat import _saved_rag_memory

        user_message = "前回なんの話をしたっけ？"
        from app.memory.memory_policy import resolved_memory_policy

        policy = MagicMock(name="resolved_memory_policy")
        policy.privacy = resolved_memory_policy().privacy

        monkeypatch.setenv("RAG_ENABLED", "true")
        with patch(_RESOLVED_MEMORY_POLICY, return_value=policy):
            with TestClient(app) as client:
                with patch(_LOAD_PERSONALITY, return_value=_character_card()):
                    with patch(
                        _BUILD_AUGMENTED_SYSTEM_PROMPT,
                        return_value=RetrievalOutcome(
                            (
                                _saved_rag_memory("前回は畑の話をした", runtime_paths),
                            ),
                            False,
                        ),
                    ) as mock_build:
                        with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                            with client.websocket_connect(_WS_URL) as websocket:
                                websocket.send_json(
                                    {"type": "text", "message": user_message},
                                )
                                response = websocket.receive_json()

        _assert_persisted_content_frame(response, _LLM_REPLY)
        mock_build.assert_called_once_with(
            "miori",
            user_message,
            policy,
            scanner=ANY,
            classifier=ANY,
            approved_repository=ANY,
            embedder=ANY,
            chroma_path=runtime_paths.chroma_path,
            now=ANY,
            timezone="Asia/Tokyo",
        )
        assert "前回は畑の話をした" in _generated_contents(mock_gen)[1]


    def test_returns_422_when_payload_is_not_json_object(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect(_WS_URL) as websocket:
                    websocket.send_text('"hello"')
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 422,
            "detail": "WebSocket message must be a JSON object",
        }
        mock_gen.assert_not_called()


    def test_returns_422_when_payload_is_malformed_json(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect(_WS_URL) as websocket:
                    websocket.send_text("{")
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 422,
            "detail": "WebSocket message must be valid JSON",
        }
        mock_gen.assert_not_called()


    def test_returns_422_when_message_type_is_not_text(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect(_WS_URL) as websocket:
                    websocket.send_json({"type": "audio", "message": "こんにちは"})
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 422,
            "detail": "WebSocket message type must be 'text'",
        }
        mock_gen.assert_not_called()


    def test_returns_422_when_text_message_is_not_root_string(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect(_WS_URL) as websocket:
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


    def test_returns_422_when_text_message_is_empty(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect(_WS_URL) as websocket:
                    websocket.send_json({"type": "text", "message": ""})
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 422,
            "detail": "WebSocket text message must not be empty",
        }
        mock_gen.assert_not_called()


    def test_returns_404_error_and_disconnects_when_character_not_found(self, client):
        with patch(
            _LOAD_PERSONALITY,
            side_effect=FileNotFoundError("character not found"),
        ):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect(
                    f"/ws/unknown?conversation_id={CONVERSATION_ID}"
                ) as websocket:
                    response = websocket.receive_json()
                    with pytest.raises(WebSocketDisconnect):
                        websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 404,
            "detail": "Character 'unknown' not found",
        }
        mock_gen.assert_not_called()


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
        ws_url = f"/ws/miori?conversation_id={conversation.conversation_id}"

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect(ws_url) as websocket:
                    response = websocket.receive_json()
                    with pytest.raises(WebSocketDisconnect):
                        websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 404,
            "detail": "conversation was not found",
        }
        mock_gen.assert_not_called()


    def test_returns_safe_404_for_unknown_conversation_without_creating_it(
        self,
        client,
        unknown_chat_conversation,
    ):
        repository = client.app.state.conversation_history_repository

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect(_WS_URL) as websocket:
                    websocket.send_json({"type": "text", "message": "こんにちは"})
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 404,
            "detail": "conversation was not found",
        }
        assert repository.list_active_conversations("miori") == []
        mock_gen.assert_not_called()


    def test_returns_safe_404_when_conversation_is_archived_after_session_open(
        self,
        client,
    ):
        repository = client.app.state.conversation_history_repository
        conversation = repository.create_conversation("miori")
        ws_url = f"/ws/miori?conversation_id={conversation.conversation_id}"

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect(ws_url) as websocket:
                    repository.archive_conversation(
                        "miori",
                        conversation.conversation_id,
                    )
                    websocket.send_json({"type": "text", "message": "こんにちは"})
                    response = websocket.receive_json()
                    with pytest.raises(WebSocketDisconnect):
                        websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 404,
            "detail": "conversation was not found",
        }
        mock_gen.assert_not_called()


    def test_audio_handler_returns_safe_404_when_conversation_becomes_unavailable(
        self,
    ):
        import asyncio

        from app.conversation_history.errors import ConversationNotFoundError
        from app.routers.ws import _ConnectionMeasurementState, _handle_audio_payload

        class RecordingWebSocket:
            def __init__(self):
                self.sent_json = []
                self.closed = False

            async def send_json(self, payload):
                self.sent_json.append(payload)

            async def close(self):
                self.closed = True

        class MissingConversationSession:
            def generate_reply(self, message):
                raise ConversationNotFoundError()

        class CallbackAudioSession:
            def generate_response_audio(self, audio, generate_reply):
                generate_reply("保存してはいけない文字起こし")

        async def run_handler():
            websocket = RecordingWebSocket()
            keep_open = await _handle_audio_payload(
                websocket,
                asyncio.Lock(),
                MissingConversationSession(),
                CallbackAudioSession(),
                _PCM_AUDIO,
                "miori",
                _ConnectionMeasurementState(),
            )
            return keep_open, websocket

        keep_open, websocket = anyio.run(run_handler)

        assert keep_open is False
        assert websocket.sent_json == [
            {
                "type": "error",
                "status": 404,
                "detail": "conversation was not found",
            }
        ]
        assert websocket.closed is True


    def test_should_use_shared_mapping_when_opening_chat_session_fails(
        self,
        monkeypatch,
    ):
        import asyncio

        from app.chat_service import CharacterNotFoundError
        from app.routers import ws as ws_module

        error = CharacterNotFoundError("miori")
        mapped_error = (490, "mapped session error")
        map_chat_error = MagicMock(return_value=mapped_error)
        monkeypatch.setattr(ws_module, "_map_chat_error", map_chat_error)

        class MissingCharacterService:
            async def create_chat_session(self, character_name, conversation_id):
                raise error

        class RecordingWebSocket:
            def __init__(self):
                self.app = type("App", (), {})()
                self.app.state = type("State", (), {})()
                self.app.state.chat_service = MissingCharacterService()
                self.sent_json = []
                self.closed = False

            async def send_json(self, payload):
                self.sent_json.append(payload)

            async def close(self):
                self.closed = True

        async def run_open_chat_session():
            websocket = RecordingWebSocket()
            session = await ws_module._open_chat_session(
                websocket,
                asyncio.Lock(),
                "miori",
                CONVERSATION_ID,
            )
            return session, websocket

        session, websocket = anyio.run(run_open_chat_session)

        mapped_status, mapped_detail = mapped_error
        map_chat_error.assert_called_once_with(error)
        assert session is None
        assert websocket.sent_json == [
            {"type": "error", "status": mapped_status, "detail": mapped_detail}
        ]
        assert websocket.closed is True


    def test_returns_404_when_character_disappears_after_session_open(self, client):
        with patch(
            _LOAD_PERSONALITY,
            side_effect=[
                _character_card(),
                FileNotFoundError("character not found"),
            ],
        ):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with client.websocket_connect(_WS_URL) as websocket:
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


    def test_should_use_shared_mapping_for_missing_character_in_text_handler(
        self,
        monkeypatch,
    ):
        import asyncio

        from app.chat_service import CharacterNotFoundError
        from app.routers import ws as ws_module

        error = CharacterNotFoundError("miori")
        mapped_error = (490, "mapped text error")
        map_chat_error = MagicMock(return_value=mapped_error)
        monkeypatch.setattr(ws_module, "_map_chat_error", map_chat_error)

        class MissingCharacterSession:
            def generate_reply(self, message):
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
            keep_open = await ws_module._handle_text_frame(
                websocket,
                asyncio.Lock(),
                MissingCharacterSession(),
                {"text": '{"type": "text", "message": "hello"}'},
            )
            return keep_open, websocket

        keep_open, websocket = anyio.run(run_handler)

        mapped_status, mapped_detail = mapped_error
        map_chat_error.assert_called_once_with(error)
        assert keep_open is False
        assert websocket.sent_json == [
            {"type": "error", "status": mapped_status, "detail": mapped_detail}
        ]
        assert websocket.closed is True


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
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, return_value=_LLM_REPLY) as mock_gen:
                with TestClient(app) as client:
                    with client.websocket_connect(_WS_URL) as websocket:
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
        _assert_persisted_content_frame(second_response, _LLM_REPLY)
        assert _generated_contents(mock_gen)[-1] == "続けてください"


    def test_returns_504_error_when_llm_request_times_out(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _GENERATE_RESPONSE,
                side_effect=httpx.ReadTimeout("timed out"),
            ):
                with client.websocket_connect(_WS_URL) as websocket:
                    websocket.send_json({"type": "text", "message": "こんにちは"})
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 504,
            "detail": "LLM request timed out",
        }


    def test_returns_502_error_when_llm_request_fails(self, client):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _GENERATE_RESPONSE,
                side_effect=httpx.HTTPError("boom"),
            ):
                with client.websocket_connect(_WS_URL) as websocket:
                    websocket.send_json({"type": "text", "message": "こんにちは"})
                    response = websocket.receive_json()

        assert response == {
            "type": "error",
            "status": 502,
            "detail": "LLM request failed",
        }


    @pytest.mark.parametrize(
        "error_name",
        [pytest.param("timeout", id="timeout"), pytest.param("backend", id="backend")],
    )
    def test_should_use_shared_mapping_when_generating_reply_fails(
        self,
        monkeypatch,
        error_name,
    ):
        import asyncio

        from app.chat_service import ChatBackendError, ChatTimeoutError
        from app.routers import ws as ws_module

        errors = {
            "timeout": ChatTimeoutError(),
            "backend": ChatBackendError(),
        }
        error = errors[error_name]
        mapped_error = (590, "mapped reply error")
        map_chat_error = MagicMock(return_value=mapped_error)
        monkeypatch.setattr(ws_module, "_map_chat_error", map_chat_error)

        class FailingChatSession:
            def generate_reply(self, message):
                raise error

        class RecordingWebSocket:
            def __init__(self):
                self.sent_json = []

            async def send_json(self, payload):
                self.sent_json.append(payload)

        async def run_generate_reply():
            websocket = RecordingWebSocket()
            reply = await ws_module._generate_reply(
                websocket,
                asyncio.Lock(),
                FailingChatSession(),
                "hello",
            )
            return reply, websocket

        reply, websocket = anyio.run(run_generate_reply)

        mapped_status, mapped_detail = mapped_error
        map_chat_error.assert_called_once_with(error)
        assert reply is None
        assert websocket.sent_json == [
            {"type": "error", "status": mapped_status, "detail": mapped_detail}
        ]


    @pytest.mark.parametrize(
        ("llm_error", "expected_response"),
        [
            pytest.param(
                httpx.ReadTimeout("timed out"),
                {
                    "type": "error",
                    "status": 504,
                    "detail": "LLM request timed out",
                },
                id="timeout",
            ),
            pytest.param(
                httpx.HTTPError("backend failed"),
                {
                    "type": "error",
                    "status": 502,
                    "detail": "LLM request failed",
                },
                id="backend",
            ),
        ],
    )
    def test_should_keep_text_connection_open_after_llm_error(
        self,
        client,
        llm_error,
        expected_response,
    ):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(
                _GENERATE_RESPONSE,
                side_effect=[llm_error, _LLM_REPLY],
            ):
                with client.websocket_connect(_WS_URL) as websocket:
                    websocket.send_json({"type": "text", "message": "1回目"})
                    first_response = websocket.receive_json()

                    websocket.send_json({"type": "text", "message": "2回目"})
                    second_response = websocket.receive_json()

        assert first_response == expected_response
        _assert_persisted_content_frame(second_response, _LLM_REPLY)


    @pytest.mark.parametrize(
        ("llm_error", "expected_response"),
        [
            pytest.param(
                httpx.ReadTimeout("timed out"),
                {
                    "type": "error",
                    "status": 504,
                    "detail": "LLM request timed out",
                },
                id="timeout",
            ),
            pytest.param(
                httpx.HTTPError("backend failed"),
                {
                    "type": "error",
                    "status": 502,
                    "detail": "LLM request failed",
                },
                id="backend",
            ),
        ],
    )
    def test_should_return_mapped_audio_chat_error_and_keep_connection_open(
        self,
        client,
        llm_error,
        expected_response,
    ):
        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_LOAD_TTS_CONFIG, return_value=_tts_config()):
                with patch(_TRANSCRIBE, return_value="audio question"):
                    with patch(
                        _GENERATE_RESPONSE,
                        side_effect=[llm_error, _LLM_REPLY],
                    ) as mock_gen:
                        with patch(
                            _SYNTHESIZE,
                            return_value=b"RIFF output",
                        ) as mock_tts:
                            with client.websocket_connect(_WS_URL) as websocket:
                                websocket.send_bytes(_PCM_AUDIO)
                                audio_error = websocket.receive_json()

                                websocket.send_json(
                                    {"type": "text", "message": "continue"},
                                )
                                text_response = websocket.receive_json()

        assert audio_error == expected_response
        _assert_persisted_content_frame(text_response, _LLM_REPLY)
        assert _generated_user_messages(mock_gen) == [
            "audio question",
            "continue",
        ]
        mock_tts.assert_not_called()


class TestWebSocketFlow:
    def test_rag_disabled_restores_only_same_websocket_conversation_history(
        self,
        monkeypatch,
        conversation_history_database_path,
    ):
        monkeypatch.setenv("RAG_ENABLED", "false")
        target_user = "password: websocket-target-user-secret"
        target_assistant = "password: websocket-target-assistant-secret"
        current_user = "対象会話の前の応答を確認して"
        other_conversation_user = "別会話の内容"
        other_character_user = "別キャラクターの内容"

        def generate(prompt, *, max_output_tokens, settings):
            del max_output_tokens, settings
            current = prompt.messages[-1].content
            if current == target_user:
                return target_assistant
            return "確認しました"

        with patch(_LOAD_PERSONALITY, return_value=_character_card()):
            with patch(_GENERATE_RESPONSE, side_effect=generate) as mock_generate:
                with TestClient(app) as client:
                    with client.websocket_connect(
                        f"/ws/miori?conversation_id={OTHER_CONVERSATION_ID}"
                    ) as websocket:
                        websocket.send_json(
                            {"type": "text", "message": other_conversation_user}
                        )
                        websocket.receive_json()
                    with client.websocket_connect(
                        f"/ws/other?conversation_id={CONVERSATION_ID}"
                    ) as websocket:
                        websocket.send_json(
                            {"type": "text", "message": other_character_user}
                        )
                        websocket.receive_json()
                    with client.websocket_connect(_WS_URL) as websocket:
                        websocket.send_json({"type": "text", "message": target_user})
                        websocket.receive_json()
                        websocket.send_json({"type": "text", "message": current_user})
                        response = websocket.receive_json()

        _assert_persisted_content_frame(response, "確認しました")
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

    def test_path_character_prompt_and_message_reach_ollama_payload(
        self, client, tmp_path, monkeypatch
    ):
        import app.characters.loader as loader_module

        system_prompt = "# 光織\nあなたは光織です。"
        _write_character(tmp_path, "miori", system_prompt)
        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: tmp_path)

        expected_reply = "光織です。よろしくお願いします。"
        with patch(
            "app.inference.adapters.ollama.OllamaAdapter.generate_text",
            return_value=ProviderTextResult(text=expected_reply, usage=None),
        ) as generate_text:
            with client.websocket_connect(
                f"/ws/miori?conversation_id={CONVERSATION_ID}&character=ignored&message=ignored",
            ) as websocket:
                websocket.send_json(
                    {"type": "text", "message": "自己紹介してください"},
                )
                response = websocket.receive_json()

        _assert_persisted_content_frame(response, expected_reply)

        request = generate_text.call_args.args[-1]
        assert [(message.role, message.content) for message in request.messages] == [
            ("system", f"## 応答方針\n{system_prompt}"),
            ("user", "自己紹介してください"),
        ]


def test_audio_measurement_metadata_records_pre_response_events() -> None:
    from app.routers.ws import (
        _extract_audio_correlation,
        _record_audio_request_events,
    )
    from app.voice_metrics import MeasurementContext

    recorded = []
    correlation = _extract_audio_correlation(
        {
            "type": "audio_metadata",
            "event_id": "01992f57-8c65-79d0-924f-e2cd79bc03ef",
            "session_id": "01992f57-8c65-79d0-924f-e2cd79bc01cd",
            "utterance_id": "01992f57-8c65-79d0-924f-e2cd79bc02de",
            "captured_audio_start_client_ms": 100.0,
            "vad_speech_end_client_ms": 200.0,
            "utterance_finalized_client_ms": 225.0,
            "response_decision_client_ms": 230.0,
            "required_manual_operations": 0,
        },
        None,
    )
    measurement = MeasurementContext(
        measurement_kind="automated_test",
        character_id="miori",
        session_id=correlation.session_id,
        utterance_id=correlation.utterance_id,
        response_id="01992f57-8c65-79d0-924f-e2cd79bc04fa",
        record=recorded.append,
        clock_ns=lambda: 0,
    )

    _record_audio_request_events(measurement, correlation, 1_000_000)

    assert {event.name for event in recorded} == {
        "user_audio_received",
        "captured_audio_start",
        "vad_speech_end",
        "utterance_finalized",
        "response_decision",
        "manual_operations",
    }
    manual = next(event for event in recorded if event.name == "manual_operations")
    assert manual.value == 0
    assert next(
        event for event in recorded if event.name == "user_audio_received"
    ).clock_domain == "server_monotonic"


def test_id_only_audio_metadata_keeps_optional_measurements_missing() -> None:
    from app.routers.ws import _extract_audio_correlation

    correlation = _extract_audio_correlation(
        {
            "type": "audio_metadata",
            "event_id": "01992f57-8c65-79d0-924f-e2cd79bc03ef",
            "session_id": "01992f57-8c65-79d0-924f-e2cd79bc01cd",
            "utterance_id": "01992f57-8c65-79d0-924f-e2cd79bc02de",
        },
        None,
    )

    assert correlation.measurement is None

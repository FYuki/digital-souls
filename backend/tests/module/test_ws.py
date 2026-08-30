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

_LOAD_PERSONALITY = "app.main.load_character_card"
_GENERATE_RESPONSE = "app.llm.router.generate_response"
_COUNT_INPUT_TOKENS = "app.llm.router.count_input_tokens"
_BUILD_AUGMENTED_SYSTEM_PROMPT = (
    "app._chat_runtime._rag_service.retrieve_prompt_memories"
)
_RESOLVED_MEMORY_POLICY = "app.main.resolved_memory_policy"
_LOAD_TTS_CONFIG = "app.audio_pipeline.load_tts_config"
_TRANSCRIBE = "app.stt.remote_whisper_client.RemoteWhisperTranscriber.transcribe"
_SYNTHESIZE = "app.tts.voicevox_client.VoicevoxClient.synthesize"
_BUILD_PROMPT = "app.chat_prompt.PromptBuilder.build"

_PERSONALITY = "# 光織\n穏やかなAIです。"
_LLM_REPLY = "光織です。よろしくお願いします。"


@pytest.fixture(autouse=True)
def _formal_token_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _COUNT_INPUT_TOKENS, lambda messages, *, settings: len(messages)
    )
_PCM_AUDIO = b"\x01\x00\x02\x00"
_ODD_LENGTH_PCM_AUDIO = b"\x01\x00\x03"
_TTS_CONFIG_MISSING_MESSAGE = "'tts_config' field is missing in character card data"
_WS_URL = f"/ws/miori?conversation_id={CONVERSATION_ID}"

pytestmark = pytest.mark.usefixtures("existing_chat_conversations")


class _StubDeliverySession:
    def mark_delivered(self, turn_id):
        return None

    def mark_delivery_failed(self, turn_id):
        return None

    def close(self):
        return None


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


def _generated_contents(generate: MagicMock) -> list[str]:
    prompt = generate.call_args.args[0]
    return [message.content for message in prompt.messages]


def _generated_user_messages(generate: MagicMock) -> list[str]:
    return [
        next(
            message.content
            for message in reversed(call.args[0].messages)
            if message.role.value == "user"
        )
        for call in generate.call_args_list
    ]


def _wait_for_event(event: threading.Event, label: str, timeout: float = 5.0) -> None:
    if not event.wait(timeout=timeout):
        raise AssertionError(f"{label} was not observed before timeout")


def _assert_persisted_content_frame(payload: dict, assistant_content: str) -> None:
    assert set(payload) == {"type", "turn"}
    assert payload["type"] == "text"
    assert payload["turn"]["kind"] == "content"
    assert payload["turn"]["assistant_content"] == assistant_content


def _persisted_content_frame(assistant_content: str) -> dict:
    return {
        "type": "text",
        "turn": {
            "kind": "content",
            "turn_id": str(TURN_ID),
            "user_content": "saved user content",
            "assistant_content": assistant_content,
        },
    }


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
        character,
        character_card_document(data=data),
    )


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


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
                                MemorySearchResult(
                                memory_id="memory-1",
                                normalized_text="前回は畑の話をした",
                                occurred_at="2026-07-31T00:00:00.000000Z",
                                memory_type="USER_PREFERENCE",
                                raw_distance=1.25,
                                ),
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
            "app.llm.ollama_client.httpx.post",
            return_value=_ollama_response(expected_reply),
        ) as mock_post:
            with client.websocket_connect(
                f"/ws/miori?conversation_id={CONVERSATION_ID}&character=ignored&message=ignored",
            ) as websocket:
                websocket.send_json(
                    {"type": "text", "message": "自己紹介してください"},
                )
                response = websocket.receive_json()

        _assert_persisted_content_frame(response, expected_reply)

        payload = mock_post.call_args.kwargs["json"]
        assert payload["messages"] == [
            {"role": "system", "content": f"## 応答方針\n{system_prompt}"},
            {"role": "user", "content": "自己紹介してください"},
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

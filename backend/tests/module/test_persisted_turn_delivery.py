from __future__ import annotations

from unittest.mock import patch

from app.audio_pipeline import AudioPipelineService


RAW_USER = "password: raw-user-secret-41"
RAW_ASSISTANT = "確認: password: raw-assistant-secret-42"
MASKED_USER = "password: [PASSWORD]"
MASKED_ASSISTANT = "確認: password: [PASSWORD]"
STORAGE_OPT_OUT = "このターンは履歴に残さないで"


class _StubTranscriber:
    def __init__(self, transcript: str) -> None:
        self._transcript = transcript

    def transcribe(self, audio: bytes) -> str:
        assert audio == b"\x01\x00"
        return self._transcript


class _RecordingSpeechSynthesizer:
    def __init__(self) -> None:
        self.synthesize_calls: list[tuple[str, int]] = []

    def synthesize(self, text: str, speaker_id: int) -> bytes:
        self.synthesize_calls.append((text, speaker_id))
        return b"RIFF saved-turn"

    def close(self) -> None:
        return None


def _create_conversation(client):  # type: ignore[no-untyped-def]
    repository = client.app.state.conversation_history_repository
    return repository.create_conversation("miori")


def _chat_body(conversation_id, message: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {
        "character": "miori",
        "conversation_id": str(conversation_id),
        "message": message,
    }


def _assert_content_turn(turn: dict[str, object]) -> None:
    assert set(turn) == {
        "kind",
        "turn_id",
        "user_content",
        "assistant_content",
    }
    assert turn["kind"] == "content"
    assert turn["user_content"] == MASKED_USER
    assert turn["assistant_content"] == MASKED_ASSISTANT
    assert RAW_USER not in repr(turn)
    assert RAW_ASSISTANT not in repr(turn)


def _assert_privacy_skipped_turn(turn: dict[str, object]) -> None:
    assert set(turn) == {
        "kind",
        "turn_id",
        "reason_code",
        "sanitizer_version",
        "policy_version",
    }
    assert turn["kind"] == "privacy_skipped"
    assert turn["reason_code"] == "STORAGE_OPT_OUT"
    assert STORAGE_OPT_OUT not in repr(turn)


def test_should_return_the_persisted_masked_turn_from_http_chat(client) -> None:
    conversation = _create_conversation(client)

    with patch("app.main.generate_response", return_value=RAW_ASSISTANT):
        response = client.post(
            "/chat",
            json=_chat_body(conversation.conversation_id, RAW_USER),
        )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"character", "turn"}
    assert payload["character"] == "miori"
    _assert_content_turn(payload["turn"])


def test_should_return_metadata_only_when_http_chat_storage_is_skipped(client) -> None:
    conversation = _create_conversation(client)

    with patch("app.main.generate_response", return_value=RAW_ASSISTANT):
        response = client.post(
            "/chat",
            json=_chat_body(conversation.conversation_id, STORAGE_OPT_OUT),
        )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"character", "turn"}
    assert payload["character"] == "miori"
    _assert_privacy_skipped_turn(payload["turn"])


def test_should_send_the_persisted_masked_turn_from_text_websocket(client) -> None:
    conversation = _create_conversation(client)

    with patch("app.main.generate_response", return_value=RAW_ASSISTANT):
        with client.websocket_connect(
            f"/ws/miori?conversation_id={conversation.conversation_id}"
        ) as websocket:
            websocket.send_json({"type": "text", "message": RAW_USER})
            payload = websocket.receive_json()

    assert set(payload) == {"type", "turn"}
    assert payload["type"] == "text"
    _assert_content_turn(payload["turn"])


def test_should_send_metadata_only_for_privacy_skipped_text_websocket(client) -> None:
    conversation = _create_conversation(client)

    with patch("app.main.generate_response", return_value=RAW_ASSISTANT):
        with client.websocket_connect(
            f"/ws/miori?conversation_id={conversation.conversation_id}"
        ) as websocket:
            websocket.send_json({"type": "text", "message": STORAGE_OPT_OUT})
            payload = websocket.receive_json()

    assert set(payload) == {"type", "turn"}
    assert payload["type"] == "text"
    _assert_privacy_skipped_turn(payload["turn"])


def test_should_synthesize_only_the_persisted_masked_assistant_content(client) -> None:
    conversation = _create_conversation(client)
    synthesizer = _RecordingSpeechSynthesizer()
    client.app.state.audio_pipeline_service = AudioPipelineService(
        _StubTranscriber(RAW_USER),
        synthesizer,
    )

    with patch("app.main.generate_response", return_value=RAW_ASSISTANT):
        with client.websocket_connect(
            f"/ws/miori?conversation_id={conversation.conversation_id}"
        ) as websocket:
            websocket.send_bytes(b"\x01\x00")
            websocket.receive_json()

    assert synthesizer.synthesize_calls == [(MASKED_ASSISTANT, 14)]


def test_should_not_synthesize_content_for_privacy_skipped_audio_turn(client) -> None:
    conversation = _create_conversation(client)
    synthesizer = _RecordingSpeechSynthesizer()
    client.app.state.audio_pipeline_service = AudioPipelineService(
        _StubTranscriber(STORAGE_OPT_OUT),
        synthesizer,
    )

    with patch("app.main.generate_response", return_value=RAW_ASSISTANT):
        with client.websocket_connect(
            f"/ws/miori?conversation_id={conversation.conversation_id}"
        ) as websocket:
            websocket.send_bytes(b"\x01\x00")
            websocket.receive_json()

    assert synthesizer.synthesize_calls == []

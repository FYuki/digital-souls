import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.chat_service import ChatReply, PersistedContentTurn
from app.routers.chat import router as chat_router
from app.audio_pipeline import AudioPipelineStepError
from app.routers.ws import router as ws_router
from tests.conversation_history_test_support import CONVERSATION_ID, TURN_ID


UUID_V1 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


def _persisted_reply() -> ChatReply:
    turn = PersistedContentTurn(
        turn_id=TURN_ID,
        user_content="保存済みの質問",
        assistant_content="完全な回答",
    )
    return ChatReply(TURN_ID, turn)


def _chat_client(service: MagicMock) -> TestClient:
    app = FastAPI()
    app.state.chat_service = service
    app.include_router(chat_router)
    return TestClient(app, raise_server_exceptions=False)


def _valid_body() -> dict[str, str]:
    return {
        "character": "miori",
        "conversation_id": str(CONVERSATION_ID),
        "message": "こんにちは",
    }


@pytest.mark.parametrize(
    ("conversation_value", "expected_status"),
    [
        (None, 422),
        ("not-a-uuid", 422),
        (UUID_V1, 422),
    ],
)
def test_should_reject_invalid_http_conversation_id_before_service_call(
    conversation_value: str | None,
    expected_status: int,
) -> None:
    service = MagicMock()
    body = _valid_body()
    if conversation_value is None:
        del body["conversation_id"]
    else:
        body["conversation_id"] = conversation_value

    with _chat_client(service) as client:
        response = client.post("/chat", json=body)

    assert response.status_code == expected_status
    assert set(response.json()) == {"detail"}
    service.generate_chat_reply.assert_not_called()


def test_should_pass_http_ids_to_the_shared_lifecycle_service() -> None:
    service = MagicMock()
    service.generate_chat_reply.return_value = _persisted_reply()

    with _chat_client(service) as client:
        response = client.post("/chat", json=_valid_body())

    assert response.status_code == 200
    assert response.json()["character"] == "miori"
    assert response.json()["turn"]["assistant_content"] == "完全な回答"
    service.generate_chat_reply.assert_called_once_with(
        "miori",
        CONVERSATION_ID,
        "こんにちは",
    )


def test_should_not_echo_user_content_when_conversation_id_is_missing() -> None:
    raw_secret = "password: raw-validation-secret-94"
    service = MagicMock()
    body = _valid_body()
    body["message"] = raw_secret
    del body["conversation_id"]

    with _chat_client(service) as client:
        response = client.post("/chat", json=body)

    assert response.status_code == 422
    assert raw_secret not in response.text
    service.generate_chat_reply.assert_not_called()


def _ws_client(service: AsyncMock) -> TestClient:
    app = FastAPI()
    app.state.chat_service = service
    app.include_router(ws_router)
    return TestClient(app)


@pytest.mark.parametrize(
    "url",
    [
        "/ws/miori",
        "/ws/miori?conversation_id=not-a-uuid",
        f"/ws/miori?conversation_id={UUID_V1}",
    ],
)
def test_should_reject_invalid_websocket_conversation_id_before_session_creation(
    url: str,
) -> None:
    service = AsyncMock()

    with _ws_client(service) as client:
        with pytest.raises(WebSocketDisconnect) as captured:
            with client.websocket_connect(url):
                pass

    assert captured.value.code == 1008
    service.create_chat_session.assert_not_awaited()


def test_should_bind_websocket_session_to_valid_character_conversation_ids() -> None:
    service = AsyncMock()
    service.create_chat_session.return_value = MagicMock()

    with _ws_client(service) as client:
        with client.websocket_connect(
            f"/ws/miori?conversation_id={CONVERSATION_ID}"
        ):
            pass

    service.create_chat_session.assert_awaited_once_with(
        "miori",
        UUID(str(CONVERSATION_ID)),
    )


class _DeliverySession:
    def __init__(self) -> None:
        self.delivered: list[UUID] = []
        self.failed: list[UUID] = []

    def generate_reply(self, message: str) -> ChatReply:
        assert message == "こんにちは"
        return _persisted_reply()

    def mark_delivered(self, turn_id: UUID) -> None:
        self.delivered.append(turn_id)

    def mark_delivery_failed(self, turn_id: UUID) -> None:
        self.failed.append(turn_id)


class _RecordingWebSocket:
    def __init__(self, *, fail_send: bool) -> None:
        self.fail_send = fail_send
        self.sent: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        if self.fail_send:
            raise RuntimeError("synthetic send failure")
        self.sent.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        raise AssertionError("audio must not be sent after TTS failure")


class _AudioResponseSession:
    def generate_response_audio(self, audio: bytes, reply_generator):
        assert audio == b"\x01\x00"
        reply = reply_generator("こんにちは")
        return "こんにちは", reply, b"RIFF response"


class _StageFailingWebSocket:
    def __init__(self, failing_stage: str | None) -> None:
        self.failing_stage = failing_stage
        self.sent: list[tuple[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        stage = "persisted_turn"
        if self.failing_stage == stage:
            raise RuntimeError(f"synthetic {stage} send failure")
        self.sent.append((stage, payload))

    async def send_bytes(self, payload: bytes) -> None:
        if self.failing_stage == "audio_bytes":
            raise RuntimeError("synthetic audio_bytes send failure")
        self.sent.append(("audio_bytes", payload))


def test_should_acknowledge_text_turn_only_after_websocket_send_succeeds() -> None:
    from app.routers.ws import _handle_text_frame

    session = _DeliverySession()
    websocket = _RecordingWebSocket(fail_send=False)

    keep_open = anyio.run(
        _handle_text_frame,
        websocket,
        asyncio.Lock(),
        session,
        {"text": '{"type":"text","message":"こんにちは"}'},
    )

    assert keep_open is True
    assert websocket.sent[0]["type"] == "text"
    assert websocket.sent[0]["turn"]["assistant_content"] == "完全な回答"
    assert session.delivered == [TURN_ID]
    assert session.failed == []


def test_should_fail_generated_text_turn_when_websocket_send_fails() -> None:
    from app.routers.ws import _handle_text_frame

    session = _DeliverySession()
    websocket = _RecordingWebSocket(fail_send=True)

    with pytest.raises(RuntimeError, match="synthetic send failure"):
        anyio.run(
            _handle_text_frame,
            websocket,
            asyncio.Lock(),
            session,
            {"text": '{"type":"text","message":"こんにちは"}'},
        )

    assert session.delivered == []
    assert session.failed == [TURN_ID]


def test_should_fail_generated_audio_turn_when_tts_fails() -> None:
    from app.routers.ws import _ConnectionMeasurementState, _handle_audio_payload

    class FailingTtsSession:
        def generate_response_audio(self, audio: bytes, reply_generator):
            assert audio == b"\x01\x00"
            generated = reply_generator("こんにちは")
            assert generated.turn_id == TURN_ID
            raise AudioPipelineStepError(502, "VOICEVOX request failed")

    session = _DeliverySession()
    websocket = _RecordingWebSocket(fail_send=False)

    keep_open = anyio.run(
        _handle_audio_payload,
        websocket,
        asyncio.Lock(),
        session,
        FailingTtsSession(),
        b"\x01\x00",
        "miori",
        _ConnectionMeasurementState(),
    )

    assert keep_open is True
    assert websocket.sent == [
        {"type": "error", "status": 502, "detail": "VOICEVOX request failed"}
    ]
    assert session.delivered == []
    assert session.failed == [TURN_ID]


@pytest.mark.parametrize(
    ("failing_stage", "sent_stages"),
    [
        pytest.param("persisted_turn", [], id="persisted-turn"),
        pytest.param("audio_bytes", ["persisted_turn"], id="audio-bytes"),
    ],
)
def test_should_fail_generated_audio_turn_at_each_send_stage(
    failing_stage: str,
    sent_stages: list[str],
) -> None:
    from app.routers.ws import _ConnectionMeasurementState, _handle_audio_payload

    session = _DeliverySession()
    websocket = _StageFailingWebSocket(failing_stage)

    with pytest.raises(RuntimeError, match=f"synthetic {failing_stage} send failure"):
        anyio.run(
            _handle_audio_payload,
            websocket,
            asyncio.Lock(),
            session,
            _AudioResponseSession(),
            b"\x01\x00",
            "miori",
            _ConnectionMeasurementState(),
        )

    assert [stage for stage, _ in websocket.sent] == sent_stages
    assert session.delivered == []
    assert session.failed == [TURN_ID]


def test_should_acknowledge_audio_turn_only_after_all_send_stages_succeed() -> None:
    from app.routers.ws import _ConnectionMeasurementState, _handle_audio_payload

    session = _DeliverySession()
    websocket = _StageFailingWebSocket(None)

    keep_open = anyio.run(
        _handle_audio_payload,
        websocket,
        asyncio.Lock(),
        session,
        _AudioResponseSession(),
        b"\x01\x00",
        "miori",
        _ConnectionMeasurementState(),
    )

    assert keep_open is True
    assert [stage for stage, _ in websocket.sent] == [
        "persisted_turn",
        "audio_bytes",
    ]
    assert session.delivered == [TURN_ID]
    assert session.failed == []


def test_should_close_pending_turns_when_websocket_disconnects() -> None:
    from app.routers.ws import websocket_chat

    session = MagicMock()
    service = AsyncMock()
    service.create_chat_session.return_value = session

    class DisconnectingWebSocket:
        def __init__(self) -> None:
            self.app = SimpleNamespace(state=SimpleNamespace(chat_service=service))

        async def accept(self) -> None:
            return None

        async def receive(self) -> dict[str, object]:
            return {"type": "websocket.disconnect", "code": 1001, "reason": "bye"}

    anyio.run(
        websocket_chat,
        DisconnectingWebSocket(),
        "miori",
        CONVERSATION_ID,
    )

    service.create_chat_session.assert_awaited_once_with("miori", CONVERSATION_ID)
    session.close.assert_called_once_with()


def test_should_not_terminalize_a_turn_twice_when_send_failure_and_close_overlap() -> None:
    from app.routers.ws import websocket_chat

    class PendingSession(_DeliverySession):
        def __init__(self) -> None:
            super().__init__()
            self.pending: set[UUID] = set()
            self.close_calls = 0

        def generate_reply(self, message: str) -> ChatReply:
            reply = super().generate_reply(message)
            self.pending.add(reply.turn_id)
            return reply

        def mark_delivered(self, turn_id: UUID) -> None:
            self.pending.remove(turn_id)
            super().mark_delivered(turn_id)

        def mark_delivery_failed(self, turn_id: UUID) -> None:
            if turn_id in self.pending:
                self.pending.remove(turn_id)
                super().mark_delivery_failed(turn_id)

        def close(self) -> None:
            self.close_calls += 1
            self.failed.extend(self.pending)
            self.pending.clear()

    session = PendingSession()
    service = AsyncMock()
    service.create_chat_session.return_value = session

    class FailingWebSocket:
        def __init__(self) -> None:
            self.app = SimpleNamespace(state=SimpleNamespace(chat_service=service))
            self.frames = [
                {"text": '{"type":"text","message":"こんにちは"}'},
            ]

        async def accept(self) -> None:
            return None

        async def receive(self) -> dict[str, object]:
            if self.frames:
                return self.frames.pop(0)
            return {"type": "websocket.disconnect", "code": 1001, "reason": "bye"}

        async def send_json(self, payload: dict[str, object]) -> None:
            raise RuntimeError("synthetic send failure")

        async def send_bytes(self, payload: bytes) -> None:
            raise AssertionError("text flow must not send audio")

    with pytest.raises(RuntimeError, match="synthetic send failure"):
        anyio.run(
            websocket_chat,
            FailingWebSocket(),
            "miori",
            CONVERSATION_ID,
        )

    assert session.failed == [TURN_ID]
    assert session.delivered == []
    assert session.pending == set()
    assert session.close_calls == 1


def test_should_fail_late_generated_turn_once_when_disconnect_closes_session() -> None:
    from app.routers.ws import websocket_chat

    generation_started = threading.Event()
    generation_finished = threading.Event()
    release_generation = threading.Event()

    class RacingSession(_DeliverySession):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False
            self.close_calls = 0

        def generate_reply(self, message: str) -> ChatReply:
            generation_started.set()
            try:
                if not release_generation.wait(timeout=2):
                    raise RuntimeError("test generation release timed out")
                reply = super().generate_reply(message)
                if self.closed and reply.turn_id not in self.failed:
                    self.failed.append(reply.turn_id)
                return reply
            finally:
                generation_finished.set()

        def close(self) -> None:
            self.close_calls += 1
            self.closed = True
            release_generation.set()

    session = RacingSession()
    service = AsyncMock()
    service.create_chat_session.return_value = session

    class DisconnectDuringGenerationWebSocket:
        def __init__(self) -> None:
            self.app = SimpleNamespace(state=SimpleNamespace(chat_service=service))
            self.frames = [{"bytes": b"\x01\x00"}]

        async def accept(self) -> None:
            return None

        async def receive(self) -> dict[str, object]:
            if self.frames:
                return self.frames.pop(0)
            while not generation_started.is_set():
                await anyio.sleep(0)
            return {"type": "websocket.disconnect", "code": 1001, "reason": "bye"}

        async def send_json(self, payload: dict[str, object]) -> None:
            raise AssertionError("disconnected client must not receive generated content")

        async def send_bytes(self, payload: bytes) -> None:
            raise AssertionError("disconnected client must not receive generated audio")

    class BlockingAudioSession:
        def generate_response_audio(self, audio: bytes, reply_generator):
            reply = reply_generator("こんにちは")
            return "こんにちは", reply, b"RIFF response"

    websocket = DisconnectDuringGenerationWebSocket()
    websocket.app.state.audio_pipeline_service = SimpleNamespace(
        create_session=lambda character: BlockingAudioSession()
    )

    anyio.run(
        websocket_chat,
        websocket,
        "miori",
        CONVERSATION_ID,
    )

    assert generation_finished.wait(timeout=2)
    assert session.close_calls == 1
    assert session.failed == [TURN_ID]
    assert session.delivered == []

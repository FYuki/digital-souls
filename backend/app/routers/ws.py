import asyncio
import json
import logging
import threading
from contextlib import suppress
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Literal, assert_never, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, WebSocket
from pydantic import UUID4
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from app.audio.constants import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
)
from app.chat_service import (
    CharacterNotFoundError,
    ChatBackendError,
    ChatInputLimitError,
    ChatReply,
    ChatReplySession,
    ChatTimeoutError,
)
from app.conversation_history.errors import ConversationNotFoundError
from app.audio_pipeline import (
    AudioPipelineConfigError,
    AudioPipelineService,
    AudioPipelineSession,
    AudioPipelineStepError,
)
from app.routers.conversation_contracts import persisted_turn_response
from app.routers.validation import CONVERSATION_NOT_FOUND_DETAIL
from app.voice_metrics import MeasurementContext, MeasurementKind, TraceEvent

router = APIRouter()
logger = logging.getLogger(__name__)

MESSAGE_TYPE_FIELD = "type"
MESSAGE_FIELD = "message"
TURN_FIELD = "turn"
STATUS_FIELD = "status"
DETAIL_FIELD = "detail"
TEXT_MESSAGE_TYPE = "text"
AUDIO_METADATA_MESSAGE_TYPE = "audio_metadata"
AUDIO_RESPONSE_METADATA_MESSAGE_TYPE = "audio_response_metadata"
MEASUREMENT_EVENT_MESSAGE_TYPE = "measurement_event"
ERROR_MESSAGE_TYPE = "error"
WEBSOCKET_TEXT_FIELD = "text"
WEBSOCKET_BYTES_FIELD = "bytes"
WEBSOCKET_TYPE_FIELD = "type"
WEBSOCKET_DISCONNECT_TYPE = "websocket.disconnect"
MAX_AUDIO_FRAME_SECONDS = 30
MAX_AUDIO_FRAME_BYTES = (
    PCM_SAMPLE_RATE_HZ
    * PCM_CHANNELS
    * PCM_SAMPLE_WIDTH_BYTES
    * MAX_AUDIO_FRAME_SECONDS
)
AUDIO_FRAME_TOO_LARGE_CLOSE_CODE = 4008
AUDIO_FRAME_TOO_LARGE_REASON = "Audio frame too large"
UNEXPECTED_AUDIO_WORKER_ERROR = "Audio processing failed"
WebSocketFrame = dict[str, object]


@dataclass(frozen=True)
class AudioRequestMeasurement:
    captured_audio_start_client_ms: float
    vad_speech_end_client_ms: float
    utterance_finalized_client_ms: float
    response_decision_client_ms: float
    required_manual_operations: int


@dataclass(frozen=True)
class AudioCorrelation:
    event_id: str
    session_id: str
    utterance_id: str
    measurement: AudioRequestMeasurement | None


@dataclass(frozen=True)
class QueuedAudioFrame:
    audio: bytes
    correlation: AudioCorrelation | None
    received_at_ns: int


AudioFrameQueue = asyncio.Queue[bytes | QueuedAudioFrame]


@dataclass(frozen=True)
class _IssuedResponse:
    session_id: str
    utterance_id: str


class _ConnectionMeasurementState:
    def __init__(self) -> None:
        self._issued: dict[str, _IssuedResponse] = {}
        self._consumed_event_ids: set[str] = set()
        self._consumed_names: set[tuple[str, str]] = set()

    def issue(self, response_id: str, correlation: AudioCorrelation) -> None:
        self._issued[response_id] = _IssuedResponse(
            session_id=correlation.session_id,
            utterance_id=correlation.utterance_id,
        )

    def revoke(self, response_id: str) -> None:
        del self._issued[response_id]

    def consume(self, event: TraceEvent) -> None:
        issued = self._issued.get(event.response_id)
        if issued is None:
            raise WebSocketMessageError("measurement response_id was not issued")
        if event.session_id != issued.session_id:
            raise WebSocketMessageError("measurement session_id does not match response")
        if event.utterance_id != issued.utterance_id:
            raise WebSocketMessageError("measurement utterance_id does not match response")
        event_name = (event.response_id, event.name)
        if event.event_id in self._consumed_event_ids or event_name in self._consumed_names:
            raise WebSocketMessageError("measurement event was already consumed")
        self._consumed_event_ids.add(event.event_id)
        self._consumed_names.add(event_name)


@dataclass(frozen=True)
class AudioFrameExtractionResult:
    keep_open: bool
    audio: bytes | None


class WebSocketMessageError(ValueError):
    """Invalid client message that should not close the WebSocket session."""


class DeliveryClosedError(RuntimeError):
    pass


class _ConnectionChatSession:
    def __init__(self, session: ChatReplySession) -> None:
        self._session = session
        self._closed = threading.Event()

    def generate_reply(self, message: str) -> ChatReply:
        reply = self._session.generate_reply(message)
        if self._closed.is_set():
            raise DeliveryClosedError()
        return reply

    def mark_delivered(self, turn_id: UUID) -> None:
        self._session.mark_delivered(turn_id)

    def mark_delivery_failed(self, turn_id: UUID) -> None:
        self._session.mark_delivery_failed(turn_id)

    def close(self) -> None:
        self._closed.set()
        self._session.close()


def _map_chat_error(
    error: (
        CharacterNotFoundError
        | ConversationNotFoundError
        | ChatInputLimitError
        | ChatTimeoutError
        | ChatBackendError
    ),
) -> tuple[int, str]:
    if isinstance(error, CharacterNotFoundError):
        return 404, error.detail
    if isinstance(error, ConversationNotFoundError):
        return 404, CONVERSATION_NOT_FOUND_DETAIL
    if isinstance(error, ChatInputLimitError):
        return 422, error.detail
    if isinstance(error, ChatTimeoutError):
        return 504, error.detail
    if isinstance(error, ChatBackendError):
        return 502, error.detail
    assert_never(error)


async def _send_json(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    payload: dict[str, object],
) -> None:
    async with send_lock:
        await _send_json_unlocked(websocket, payload)


async def _send_json_unlocked(
    websocket: WebSocket,
    payload: dict[str, object],
) -> None:
    await websocket.send_json(payload)


async def _send_bytes_unlocked(websocket: WebSocket, payload: bytes) -> None:
    await websocket.send_bytes(payload)


async def _send_error(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    status: int,
    detail: str,
) -> None:
    await _send_json(
        websocket,
        send_lock,
        {
            MESSAGE_TYPE_FIELD: ERROR_MESSAGE_TYPE,
            STATUS_FIELD: status,
            DETAIL_FIELD: detail,
        },
    )


async def _send_error_and_close(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    status: int,
    detail: str,
) -> None:
    async with send_lock:
        await _send_json_unlocked(
            websocket,
            {
                MESSAGE_TYPE_FIELD: ERROR_MESSAGE_TYPE,
                STATUS_FIELD: status,
                DETAIL_FIELD: detail,
            },
        )
        await websocket.close()


async def _receive_frame(websocket: WebSocket) -> WebSocketFrame:
    frame = cast(WebSocketFrame, await websocket.receive())
    if frame.get(WEBSOCKET_TYPE_FIELD) == WEBSOCKET_DISCONNECT_TYPE:
        raise WebSocketDisconnect(
            cast(int, frame.get("code")),
            cast(str | None, frame.get("reason")),
        )
    return frame


async def _receive_frame_while_audio_worker_runs(
    websocket: WebSocket,
    audio_worker: asyncio.Task[None],
) -> WebSocketFrame | None:
    receive_task = asyncio.create_task(_receive_frame(websocket))
    done, _ = await asyncio.wait(
        {receive_task, audio_worker},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if audio_worker in done:
        receive_task.cancel()
        with suppress(asyncio.CancelledError):
            await receive_task
        return None

    return await receive_task


def _extract_text_frame_payload(frame: WebSocketFrame) -> object:
    try:
        raw_text = frame[WEBSOCKET_TEXT_FIELD]
    except KeyError as exc:
        raise WebSocketMessageError("WebSocket frame must include text") from exc

    if not isinstance(raw_text, str):
        raise WebSocketMessageError("WebSocket text frame must be a string")

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise WebSocketMessageError("WebSocket message must be valid JSON") from exc


def _extract_text_message(payload: object) -> str:
    if not isinstance(payload, dict):
        raise WebSocketMessageError("WebSocket message must be a JSON object")

    message_type = payload.get(MESSAGE_TYPE_FIELD)
    if message_type != TEXT_MESSAGE_TYPE:
        raise WebSocketMessageError("WebSocket message type must be 'text'")

    message = payload.get(MESSAGE_FIELD)
    if not isinstance(message, str):
        raise WebSocketMessageError("WebSocket text message must include a string message")
    if not message:
        raise WebSocketMessageError("WebSocket text message must not be empty")

    return message


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise WebSocketMessageError(f"audio metadata {field} must be a UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise WebSocketMessageError(
            f"audio metadata {field} must be a UUID string"
        ) from exc
    if str(parsed) != value:
        raise WebSocketMessageError(f"audio metadata {field} must be canonical")
    return value


def _extract_audio_correlation(
    payload: object,
    connection_session_id: UUID | None,
) -> AudioCorrelation:
    if not isinstance(payload, dict) or payload.get(MESSAGE_TYPE_FIELD) != AUDIO_METADATA_MESSAGE_TYPE:
        raise WebSocketMessageError("WebSocket audio metadata is invalid")
    event_id = _canonical_uuid(payload.get("event_id"), "event_id")
    utterance_id = _canonical_uuid(payload.get("utterance_id"), "utterance_id")
    payload_session = payload.get("session_id")
    if payload_session is None:
        if connection_session_id is None:
            raise WebSocketMessageError("audio metadata session_id is required")
        session_id = str(connection_session_id)
    else:
        session_id = _canonical_uuid(payload_session, "session_id")
        if connection_session_id is not None and session_id != str(connection_session_id):
            raise WebSocketMessageError("audio metadata session_id must match the connection")
    measurement_fields = {
        "captured_audio_start_client_ms",
        "vad_speech_end_client_ms",
        "utterance_finalized_client_ms",
        "response_decision_client_ms",
        "required_manual_operations",
    }
    supplied_measurement_fields = measurement_fields.intersection(payload)
    measurement = None
    if supplied_measurement_fields:
        if supplied_measurement_fields != measurement_fields:
            raise WebSocketMessageError("audio measurement metadata is incomplete")
        numeric_values = {
            key: payload[key]
            for key in measurement_fields - {"required_manual_operations"}
        }
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
            for value in numeric_values.values()
        ):
            raise WebSocketMessageError("audio measurement timestamps must be non-negative numbers")
        manual_operations = payload["required_manual_operations"]
        if (
            isinstance(manual_operations, bool)
            or not isinstance(manual_operations, int)
            or manual_operations < 0
        ):
            raise WebSocketMessageError("required_manual_operations must be a non-negative integer")
        captured_start = float(payload["captured_audio_start_client_ms"])
        vad_end = float(payload["vad_speech_end_client_ms"])
        utterance_finalized = float(payload["utterance_finalized_client_ms"])
        response_decision = float(payload["response_decision_client_ms"])
        if not captured_start <= vad_end <= utterance_finalized <= response_decision:
            raise WebSocketMessageError("audio measurement timestamps are out of order")
        measurement = AudioRequestMeasurement(
            captured_audio_start_client_ms=captured_start,
            vad_speech_end_client_ms=vad_end,
            utterance_finalized_client_ms=utterance_finalized,
            response_decision_client_ms=response_decision,
            required_manual_operations=manual_operations,
        )
    return AudioCorrelation(
        event_id=event_id,
        session_id=session_id,
        utterance_id=utterance_id,
        measurement=measurement,
    )


def _extract_client_measurement_event(
    payload: object,
    connection_session_id: UUID | None,
    measurement_kind: MeasurementKind,
    character_id: str,
) -> TraceEvent:
    if not isinstance(payload, dict) or payload.get(MESSAGE_TYPE_FIELD) != MEASUREMENT_EVENT_MESSAGE_TYPE:
        raise WebSocketMessageError("WebSocket measurement event is invalid")
    event_id = _canonical_uuid(payload.get("event_id"), "event_id")
    session_id = _canonical_uuid(payload.get("session_id"), "session_id")
    utterance_id = _canonical_uuid(payload.get("utterance_id"), "utterance_id")
    response_id = _canonical_uuid(payload.get("response_id"), "response_id")
    if connection_session_id is not None and session_id != str(connection_session_id):
        raise WebSocketMessageError("measurement session_id must match the connection")
    name = payload.get("name")
    if name not in {"client_audio_received", "first_playback"}:
        raise WebSocketMessageError("measurement event name is invalid")
    if payload.get("clock_domain") != "client_monotonic":
        raise WebSocketMessageError("measurement clock domain is invalid")
    if payload.get("unit") != "millisecond":
        raise WebSocketMessageError("measurement unit is invalid")
    timestamp = payload.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise WebSocketMessageError("measurement timestamp must be numeric")
    try:
        return TraceEvent(
            schema_version="1.0",
            measurement_kind=measurement_kind,
            event_id=event_id,
            character_id=character_id,
            session_id=session_id,
            utterance_id=utterance_id,
            response_id=response_id,
            name=name,
            stage="transport" if name == "client_audio_received" else "playback",
            outcome="success",
            timestamp=timestamp,
            clock_domain="client_monotonic",
            unit="millisecond",
        )
    except ValueError as exc:
        raise WebSocketMessageError("measurement event is invalid") from exc


def _record_trace_event(websocket: WebSocket, event: TraceEvent) -> None:
    recorder = getattr(websocket.app.state, "voice_trace_recorder", None)
    if recorder is None:
        logger.info(
            "voice metric observed: name=%s outcome=%s",
            event.name,
            event.outcome,
        )
        return
    recorder.record(event)


def _connection_measurement_kind(websocket: WebSocket) -> MeasurementKind:
    return cast(MeasurementKind, websocket.app.state.voice_measurement_kind)


def _extract_audio_frame(frame: WebSocketFrame) -> bytes:
    try:
        audio = frame[WEBSOCKET_BYTES_FIELD]
    except KeyError as exc:
        raise WebSocketMessageError("WebSocket frame must include bytes") from exc

    if not isinstance(audio, bytes):
        raise WebSocketMessageError("WebSocket audio frame must be bytes")
    return audio


def _is_audio_frame_too_large(audio: bytes) -> bool:
    return len(audio) > MAX_AUDIO_FRAME_BYTES


async def _close_oversized_audio_frame(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    audio: bytes,
) -> bool:
    if not _is_audio_frame_too_large(audio):
        return False

    logger.error(
        "%s: received %s bytes, limit is %s bytes",
        AUDIO_FRAME_TOO_LARGE_REASON,
        len(audio),
        MAX_AUDIO_FRAME_BYTES,
    )
    async with send_lock:
        await websocket.close(
            code=AUDIO_FRAME_TOO_LARGE_CLOSE_CODE,
            reason=AUDIO_FRAME_TOO_LARGE_REASON,
        )
    return True


async def _open_chat_session(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    character_name: str,
    conversation_id: UUID,
) -> ChatReplySession | None:
    try:
        chat_service = websocket.app.state.chat_service
        chat_session = await chat_service.create_chat_session(
            character_name,
            conversation_id,
        )
        return cast(ChatReplySession, chat_session)
    except (CharacterNotFoundError, ConversationNotFoundError) as exc:
        status, detail = _map_chat_error(exc)
        await _send_error_and_close(websocket, send_lock, status, detail)
        return None


async def _generate_reply(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    chat_session: ChatReplySession,
    message: str,
) -> ChatReply | None:
    try:
        return await run_in_threadpool(chat_session.generate_reply, message)
    except (ChatInputLimitError, ChatTimeoutError, ChatBackendError) as exc:
        status, detail = _map_chat_error(exc)
        await _send_error(websocket, send_lock, status, detail)
        return None


async def _handle_text_frame(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    chat_session: ChatReplySession,
    frame: WebSocketFrame,
) -> bool:
    try:
        payload = _extract_text_frame_payload(frame)
        message = _extract_text_message(payload)
    except WebSocketMessageError as exc:
        await _send_error(websocket, send_lock, 422, str(exc))
        return True

    try:
        reply = await _generate_reply(websocket, send_lock, chat_session, message)
    except (CharacterNotFoundError, ConversationNotFoundError) as exc:
        status, detail = _map_chat_error(exc)
        await _send_error_and_close(websocket, send_lock, status, detail)
        return False

    if reply is None:
        return True

    try:
        response_payload: dict[str, object] = {
            MESSAGE_TYPE_FIELD: TEXT_MESSAGE_TYPE,
            TURN_FIELD: persisted_turn_response(reply.persisted_turn).model_dump(
                mode="json"
            ),
        }
        await _send_json(websocket, send_lock, response_payload)
    except BaseException:
        chat_session.mark_delivery_failed(reply.turn_id)
        raise
    chat_session.mark_delivered(reply.turn_id)
    return True


async def _extract_checked_audio_frame(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    frame: WebSocketFrame,
) -> AudioFrameExtractionResult:
    try:
        audio = _extract_audio_frame(frame)
    except WebSocketMessageError as exc:
        await _send_error(websocket, send_lock, 422, str(exc))
        return AudioFrameExtractionResult(keep_open=True, audio=None)

    if await _close_oversized_audio_frame(websocket, send_lock, audio):
        return AudioFrameExtractionResult(keep_open=False, audio=None)

    return AudioFrameExtractionResult(keep_open=True, audio=audio)


async def _open_audio_session(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    character_name: str,
) -> AudioPipelineSession | None:
    try:
        audio_service = cast(
            AudioPipelineService,
            websocket.app.state.audio_pipeline_service,
        )
        return cast(
            AudioPipelineSession,
            await run_in_threadpool(audio_service.create_session, character_name),
        )
    except AudioPipelineConfigError as exc:
        logger.error("Audio pipeline configuration failed: %s", exc)
        await _send_error(websocket, send_lock, 500, str(exc))
        return None


def _record_audio_request_events(
    measurement: MeasurementContext,
    correlation: AudioCorrelation,
    received_at_ns: int,
) -> None:
    def record(
        *,
        name: str,
        stage: str,
        timestamp: int | float,
        clock_domain: str,
        unit: str,
        value: float | None = None,
    ) -> None:
        measurement.record(TraceEvent(
            schema_version="1.0",
            measurement_kind=measurement.measurement_kind,
            event_id=str(uuid4()),
            character_id=measurement.character_id,
            session_id=measurement.session_id,
            utterance_id=measurement.utterance_id,
            response_id=measurement.response_id,
            name=name,
            stage=stage,
            outcome="success",
            timestamp=timestamp,
            clock_domain=clock_domain,
            unit=cast(Literal["nanosecond", "millisecond"], unit),
            value=value,
        ))

    record(
        name="user_audio_received",
        stage="transport",
        timestamp=received_at_ns,
        clock_domain="server_monotonic",
        unit="nanosecond",
    )
    if correlation.measurement is None:
        return
    capture = correlation.measurement
    client_events = (
        ("captured_audio_start", "capture", capture.captured_audio_start_client_ms),
        ("vad_speech_end", "capture", capture.vad_speech_end_client_ms),
        ("utterance_finalized", "capture", capture.utterance_finalized_client_ms),
        ("response_decision", "response", capture.response_decision_client_ms),
    )
    for name, stage, timestamp in client_events:
        record(
            name=name,
            stage=stage,
            timestamp=timestamp,
            clock_domain="client_monotonic",
            unit="millisecond",
        )
    record(
        name="manual_operations",
        stage="interaction",
        timestamp=capture.response_decision_client_ms,
        clock_domain="client_monotonic",
        unit="millisecond",
        value=float(capture.required_manual_operations),
    )


async def _handle_audio_payload(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    chat_session: ChatReplySession,
    audio_session: AudioPipelineSession,
    audio: bytes,
    character_id: str,
    measurement_state: _ConnectionMeasurementState,
    correlation: AudioCorrelation | None = None,
    received_at_ns: int | None = None,
) -> bool:
    generated_reply: ChatReply | None = None
    response_id = str(uuid4())

    measurement = None
    if correlation is not None:
        if received_at_ns is None:
            raise ValueError("correlated audio requires its server receive timestamp")
        measurement = MeasurementContext(
            measurement_kind=_connection_measurement_kind(websocket),
            character_id=character_id,
            session_id=correlation.session_id,
            utterance_id=correlation.utterance_id,
            response_id=response_id,
            record=lambda event: _record_trace_event(websocket, event),
            clock_ns=perf_counter_ns,
        )
        _record_audio_request_events(measurement, correlation, received_at_ns)

    def generate_tracked_reply(message: str) -> ChatReply:
        nonlocal generated_reply
        generated_reply = chat_session.generate_reply(message)
        return generated_reply

    try:
        if measurement is None:
            _transcript, reply, response_audio = await run_in_threadpool(
                audio_session.generate_response_audio,
                audio,
                generate_tracked_reply,
            )
        else:
            _transcript, reply, response_audio = await run_in_threadpool(
                audio_session.generate_response_audio,
                audio,
                generate_tracked_reply,
                measurement=measurement,
            )
    except AudioPipelineStepError as exc:
        if generated_reply is not None:
            chat_session.mark_delivery_failed(generated_reply.turn_id)
        await _send_error(websocket, send_lock, exc.status_code, exc.detail)
        return True
    except (CharacterNotFoundError, ConversationNotFoundError) as exc:
        status, detail = _map_chat_error(exc)
        await _send_error_and_close(websocket, send_lock, status, detail)
        return False
    except (ChatInputLimitError, ChatTimeoutError, ChatBackendError) as exc:
        status, detail = _map_chat_error(exc)
        await _send_error(websocket, send_lock, status, detail)
        return True

    if generated_reply is None:
        raise RuntimeError("audio pipeline did not generate a chat reply")

    try:
        async with send_lock:
            await _send_json_unlocked(
                websocket,
                {
                    MESSAGE_TYPE_FIELD: TEXT_MESSAGE_TYPE,
                    TURN_FIELD: persisted_turn_response(
                        reply.persisted_turn
                    ).model_dump(mode="json"),
                },
            )
            if response_audio:
                if correlation is not None:
                    measurement_state.issue(response_id, correlation)
                    try:
                        await _send_json_unlocked(
                            websocket,
                            {
                                MESSAGE_TYPE_FIELD: AUDIO_RESPONSE_METADATA_MESSAGE_TYPE,
                                "session_id": correlation.session_id,
                                "utterance_id": correlation.utterance_id,
                                "response_id": response_id,
                            },
                        )
                        await _send_bytes_unlocked(websocket, response_audio)
                    except BaseException:
                        measurement_state.revoke(response_id)
                        raise
                else:
                    await _send_bytes_unlocked(websocket, response_audio)
    except BaseException:
        chat_session.mark_delivery_failed(generated_reply.turn_id)
        raise
    chat_session.mark_delivered(generated_reply.turn_id)
    return True


def _enqueue_audio_frame(
    queue: AudioFrameQueue,
    audio: bytes,
    correlation: AudioCorrelation | None = None,
    received_at_ns: int | None = None,
) -> None:
    if queue.full():
        queue.get_nowait()
        queue.task_done()
    queued: bytes | QueuedAudioFrame = audio
    if correlation is not None:
        if received_at_ns is None:
            raise ValueError("correlated audio requires its server receive timestamp")
        queued = QueuedAudioFrame(
            audio=audio,
            correlation=correlation,
            received_at_ns=received_at_ns,
        )
    queue.put_nowait(queued)


def _discard_pending_audio_frames(queue: AudioFrameQueue) -> None:
    while not queue.empty():
        queue.get_nowait()
        queue.task_done()


async def _run_audio_worker(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    chat_session: ChatReplySession,
    character_name: str,
    audio_queue: AudioFrameQueue,
    measurement_state: _ConnectionMeasurementState,
) -> None:
    try:
        await _process_audio_queue(
            websocket,
            send_lock,
            chat_session,
            character_name,
            audio_queue,
            measurement_state,
        )
    except DeliveryClosedError:
        return
    except Exception:
        logger.exception("Audio worker failed for character '%s'", character_name)
        async with send_lock:
            await _send_json_unlocked(
                websocket,
                {
                    MESSAGE_TYPE_FIELD: ERROR_MESSAGE_TYPE,
                    STATUS_FIELD: 500,
                    DETAIL_FIELD: UNEXPECTED_AUDIO_WORKER_ERROR,
                },
            )
            await websocket.close()


async def _process_audio_queue(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    chat_session: ChatReplySession,
    character_name: str,
    audio_queue: AudioFrameQueue,
    measurement_state: _ConnectionMeasurementState,
) -> None:
    audio_session: AudioPipelineSession | None = None
    while True:
        queued = await audio_queue.get()
        audio = queued.audio if isinstance(queued, QueuedAudioFrame) else queued
        correlation = (
            queued.correlation if isinstance(queued, QueuedAudioFrame) else None
        )
        received_at_ns = (
            queued.received_at_ns if isinstance(queued, QueuedAudioFrame) else None
        )
        try:
            if audio_session is None:
                audio_session = await _open_audio_session(
                    websocket,
                    send_lock,
                    character_name,
                )
                if audio_session is None:
                    continue

            keep_open = await _handle_audio_payload(
                websocket,
                send_lock,
                chat_session,
                audio_session,
                audio,
                character_name,
                measurement_state,
                correlation,
                received_at_ns,
            )
            if not keep_open:
                return
        finally:
            audio_queue.task_done()


def _cancel_audio_worker(audio_worker: asyncio.Task[None]) -> None:
    audio_worker.cancel()


@router.websocket("/ws/{character_name}")
async def websocket_chat(
    websocket: WebSocket,
    character_name: str,
    conversation_id: UUID4,
    session_id: UUID | None = None,
) -> None:
    await websocket.accept()
    logger.info("WebSocket connected for character '%s'", character_name)

    send_lock = asyncio.Lock()
    opened_chat_session = await _open_chat_session(
        websocket,
        send_lock,
        character_name,
        conversation_id,
    )
    if opened_chat_session is None:
        return
    chat_session = _ConnectionChatSession(opened_chat_session)

    audio_queue: AudioFrameQueue = asyncio.Queue(maxsize=1)
    measurement_state = _ConnectionMeasurementState()
    audio_worker = asyncio.create_task(
        _run_audio_worker(
            websocket,
            send_lock,
            chat_session,
            character_name,
            audio_queue,
            measurement_state,
        )
    )
    pending_audio_correlation: AudioCorrelation | None = None

    try:
        while True:
            frame = await _receive_frame_while_audio_worker_runs(websocket, audio_worker)
            if frame is None:
                return
            if frame.get(WEBSOCKET_TEXT_FIELD) is not None:
                try:
                    payload = _extract_text_frame_payload(frame)
                    is_audio_metadata = (
                        isinstance(payload, dict)
                        and payload.get(MESSAGE_TYPE_FIELD) == AUDIO_METADATA_MESSAGE_TYPE
                    )
                    if is_audio_metadata:
                        pending_audio_correlation = _extract_audio_correlation(
                            payload,
                            session_id,
                        )
                        keep_open = True
                    elif (
                        isinstance(payload, dict)
                        and payload.get(MESSAGE_TYPE_FIELD)
                        == MEASUREMENT_EVENT_MESSAGE_TYPE
                    ):
                        event = _extract_client_measurement_event(
                            payload,
                            session_id,
                            _connection_measurement_kind(websocket),
                            character_name,
                        )
                        measurement_state.consume(event)
                        _record_trace_event(websocket, event)
                        keep_open = True
                    else:
                        keep_open = await _handle_text_frame(
                            websocket,
                            send_lock,
                            chat_session,
                            frame,
                        )
                except WebSocketMessageError as exc:
                    await _send_error(websocket, send_lock, 422, str(exc))
                    keep_open = True
            elif frame.get(WEBSOCKET_BYTES_FIELD) is not None:
                extraction = await _extract_checked_audio_frame(
                    websocket,
                    send_lock,
                    frame,
                )
                if extraction.audio is None:
                    keep_open = extraction.keep_open
                    if keep_open:
                        continue
                    return
                _enqueue_audio_frame(
                    audio_queue,
                    extraction.audio,
                    pending_audio_correlation,
                    perf_counter_ns(),
                )
                pending_audio_correlation = None
                keep_open = True
            else:
                await _send_error(
                    websocket,
                    send_lock,
                    422,
                    "WebSocket frame must include text or bytes",
                )
                keep_open = True

            if not keep_open:
                return
    except WebSocketDisconnect as exc:
        logger.info(
            "WebSocket disconnected for character '%s' (code=%s)",
            character_name,
            exc.code,
        )
    finally:
        chat_session.close()
        _discard_pending_audio_frames(audio_queue)
        _cancel_audio_worker(audio_worker)
        with suppress(asyncio.CancelledError):
            await audio_worker

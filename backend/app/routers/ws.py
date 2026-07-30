import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import assert_never, cast

from fastapi import APIRouter, WebSocket
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
    ChatReplySession,
    ChatTimeoutError,
)
from app.audio_pipeline import (
    AudioPipelineConfigError,
    AudioPipelineService,
    AudioPipelineSession,
    AudioPipelineStepError,
)

router = APIRouter()
logger = logging.getLogger(__name__)

MESSAGE_TYPE_FIELD = "type"
MESSAGE_FIELD = "message"
RESPONSE_FIELD = "response"
SPEAKER_FIELD = "speaker"
STATUS_FIELD = "status"
DETAIL_FIELD = "detail"
TEXT_MESSAGE_TYPE = "text"
ERROR_MESSAGE_TYPE = "error"
USER_SPEAKER = "user"
MIORI_SPEAKER = "miori"
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
AudioFrameQueue = asyncio.Queue[bytes]


@dataclass(frozen=True)
class AudioFrameExtractionResult:
    keep_open: bool
    audio: bytes | None


class WebSocketMessageError(ValueError):
    """Invalid client message that should not close the WebSocket session."""


def _map_chat_error(
    error: CharacterNotFoundError | ChatTimeoutError | ChatBackendError,
) -> tuple[int, str]:
    if isinstance(error, CharacterNotFoundError):
        return 404, error.detail
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

    return message


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
) -> ChatReplySession | None:
    try:
        chat_service = websocket.app.state.chat_service
        chat_session = await chat_service.create_chat_session(character_name)
        return cast(ChatReplySession, chat_session)
    except CharacterNotFoundError as exc:
        status, detail = _map_chat_error(exc)
        await _send_error_and_close(websocket, send_lock, status, detail)
        return None


async def _generate_reply(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    chat_session: ChatReplySession,
    message: str,
) -> str | None:
    try:
        return await run_in_threadpool(chat_session.generate_reply, message)
    except (ChatTimeoutError, ChatBackendError) as exc:
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
    except CharacterNotFoundError as exc:
        status, detail = _map_chat_error(exc)
        await _send_error_and_close(websocket, send_lock, status, detail)
        return False

    if reply is None:
        return True

    await _send_json(
        websocket,
        send_lock,
        {
            MESSAGE_TYPE_FIELD: TEXT_MESSAGE_TYPE,
            RESPONSE_FIELD: reply,
        },
    )
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


async def _handle_audio_payload(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    chat_session: ChatReplySession,
    audio_session: AudioPipelineSession,
    audio: bytes,
) -> bool:
    try:
        transcript, reply, response_audio = await run_in_threadpool(
            audio_session.generate_response_audio,
            audio,
            chat_session.generate_reply,
        )
    except AudioPipelineStepError as exc:
        await _send_error(websocket, send_lock, exc.status_code, exc.detail)
        return True
    except CharacterNotFoundError as exc:
        status, detail = _map_chat_error(exc)
        await _send_error_and_close(websocket, send_lock, status, detail)
        return False
    except (ChatTimeoutError, ChatBackendError) as exc:
        status, detail = _map_chat_error(exc)
        await _send_error(websocket, send_lock, status, detail)
        return True

    async with send_lock:
        await _send_json_unlocked(
            websocket,
            {
                MESSAGE_TYPE_FIELD: TEXT_MESSAGE_TYPE,
                SPEAKER_FIELD: USER_SPEAKER,
                MESSAGE_FIELD: transcript,
            },
        )
        await _send_json_unlocked(
            websocket,
            {
                MESSAGE_TYPE_FIELD: TEXT_MESSAGE_TYPE,
                SPEAKER_FIELD: MIORI_SPEAKER,
                RESPONSE_FIELD: reply,
            },
        )
        await _send_bytes_unlocked(websocket, response_audio)
    return True


def _enqueue_audio_frame(queue: AudioFrameQueue, audio: bytes) -> None:
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(audio)


def _discard_pending_audio_frames(queue: AudioFrameQueue) -> None:
    while not queue.empty():
        queue.get_nowait()


async def _run_audio_worker(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    chat_session: ChatReplySession,
    character_name: str,
    audio_queue: AudioFrameQueue,
) -> None:
    try:
        await _process_audio_queue(
            websocket,
            send_lock,
            chat_session,
            character_name,
            audio_queue,
        )
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
) -> None:
    audio_session: AudioPipelineSession | None = None
    while True:
        audio = await audio_queue.get()
        if audio_session is None:
            audio_session = await _open_audio_session(websocket, send_lock, character_name)
            if audio_session is None:
                continue

        keep_open = await _handle_audio_payload(
            websocket,
            send_lock,
            chat_session,
            audio_session,
            audio,
        )
        if not keep_open:
            return


def _cancel_audio_worker(audio_worker: asyncio.Task[None]) -> None:
    audio_worker.cancel()


@router.websocket("/ws/{character_name}")
async def websocket_chat(websocket: WebSocket, character_name: str) -> None:
    await websocket.accept()
    logger.info("WebSocket connected for character '%s'", character_name)

    send_lock = asyncio.Lock()
    chat_session = await _open_chat_session(websocket, send_lock, character_name)
    if chat_session is None:
        return

    audio_queue: AudioFrameQueue = asyncio.Queue(maxsize=1)
    audio_worker = asyncio.create_task(
        _run_audio_worker(
            websocket,
            send_lock,
            chat_session,
            character_name,
            audio_queue,
        )
    )

    try:
        while True:
            frame = await _receive_frame_while_audio_worker_runs(websocket, audio_worker)
            if frame is None:
                return
            if frame.get(WEBSOCKET_TEXT_FIELD) is not None:
                keep_open = await _handle_text_frame(
                    websocket,
                    send_lock,
                    chat_session,
                    frame,
                )
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
                _enqueue_audio_frame(audio_queue, extraction.audio)
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
            "WebSocket disconnected for character '%s' (code=%s, reason=%s)",
            character_name,
            exc.code,
            exc.reason,
        )
    finally:
        _discard_pending_audio_frames(audio_queue)
        _cancel_audio_worker(audio_worker)

import json
import logging
from typing import cast

from fastapi import APIRouter, WebSocket
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

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
STATUS_FIELD = "status"
DETAIL_FIELD = "detail"
TEXT_MESSAGE_TYPE = "text"
ERROR_MESSAGE_TYPE = "error"
WEBSOCKET_TEXT_FIELD = "text"
WEBSOCKET_BYTES_FIELD = "bytes"
WEBSOCKET_TYPE_FIELD = "type"
WEBSOCKET_DISCONNECT_TYPE = "websocket.disconnect"
WebSocketFrame = dict[str, object]


class WebSocketMessageError(ValueError):
    """Invalid client message that should not close the WebSocket session."""


async def _send_error(websocket: WebSocket, status: int, detail: str) -> None:
    await websocket.send_json(
        {
            MESSAGE_TYPE_FIELD: ERROR_MESSAGE_TYPE,
            STATUS_FIELD: status,
            DETAIL_FIELD: detail,
        }
    )


async def _receive_frame(websocket: WebSocket) -> WebSocketFrame:
    frame = cast(WebSocketFrame, await websocket.receive())
    if frame.get(WEBSOCKET_TYPE_FIELD) == WEBSOCKET_DISCONNECT_TYPE:
        raise WebSocketDisconnect(
            cast(int, frame.get("code")),
            cast(str | None, frame.get("reason")),
        )
    return frame


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


async def _open_chat_session(
    websocket: WebSocket,
    character_name: str,
) -> ChatReplySession | None:
    try:
        chat_service = websocket.app.state.chat_service
        chat_session = await chat_service.create_chat_session(character_name)
        return cast(ChatReplySession, chat_session)
    except CharacterNotFoundError as exc:
        await _send_error(websocket, 404, exc.detail)
        await websocket.close()
        return None


async def _generate_reply(
    websocket: WebSocket,
    chat_session: ChatReplySession,
    message: str,
) -> str | None:
    try:
        return await run_in_threadpool(chat_session.generate_reply, message)
    except ChatTimeoutError as exc:
        await _send_error(websocket, 504, exc.detail)
        return None
    except ChatBackendError as exc:
        await _send_error(websocket, 502, exc.detail)
        return None


async def _handle_text_frame(
    websocket: WebSocket,
    chat_session: ChatReplySession,
    frame: WebSocketFrame,
) -> bool:
    try:
        payload = _extract_text_frame_payload(frame)
        message = _extract_text_message(payload)
    except WebSocketMessageError as exc:
        await _send_error(websocket, 422, str(exc))
        return True

    try:
        reply = await _generate_reply(websocket, chat_session, message)
    except CharacterNotFoundError as exc:
        await _send_error(websocket, 404, exc.detail)
        await websocket.close()
        return False

    if reply is None:
        return True

    await websocket.send_json(
        {
            MESSAGE_TYPE_FIELD: TEXT_MESSAGE_TYPE,
            RESPONSE_FIELD: reply,
        }
    )
    return True


async def _handle_audio_frame(
    websocket: WebSocket,
    chat_session: ChatReplySession,
    audio_session: AudioPipelineSession,
    frame: WebSocketFrame,
) -> bool:
    try:
        audio = _extract_audio_frame(frame)
    except WebSocketMessageError as exc:
        await _send_error(websocket, 422, str(exc))
        return True

    try:
        response_audio = await run_in_threadpool(
            audio_session.generate_response_audio,
            audio,
            chat_session.generate_reply,
        )
    except AudioPipelineStepError as exc:
        await _send_error(websocket, exc.status_code, exc.detail)
        return True
    except CharacterNotFoundError as exc:
        await _send_error(websocket, 404, exc.detail)
        await websocket.close()
        return False
    except ChatTimeoutError as exc:
        await _send_error(websocket, 504, exc.detail)
        return True
    except ChatBackendError as exc:
        await _send_error(websocket, 502, exc.detail)
        return True

    await websocket.send_bytes(response_audio)
    return True


@router.websocket("/ws/{character_name}")
async def websocket_chat(websocket: WebSocket, character_name: str) -> None:
    await websocket.accept()
    logger.info("WebSocket connected for character '%s'", character_name)

    chat_session = await _open_chat_session(websocket, character_name)
    if chat_session is None:
        return

    try:
        audio_session: AudioPipelineSession | None = None
        while True:
            frame = await _receive_frame(websocket)
            if frame.get(WEBSOCKET_TEXT_FIELD) is not None:
                keep_open = await _handle_text_frame(websocket, chat_session, frame)
            elif frame.get(WEBSOCKET_BYTES_FIELD) is not None:
                if audio_session is None:
                    try:
                        audio_service = cast(
                            AudioPipelineService,
                            websocket.app.state.audio_pipeline_service,
                        )
                        audio_session = await run_in_threadpool(
                            audio_service.create_session,
                            character_name,
                        )
                    except AudioPipelineConfigError as exc:
                        logger.error("Audio pipeline configuration failed: %s", exc)
                        await _send_error(websocket, 500, str(exc))
                        continue
                keep_open = await _handle_audio_frame(
                    websocket,
                    chat_session,
                    audio_session,
                    frame,
                )
            else:
                await _send_error(websocket, 422, "WebSocket frame must include text or bytes")
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

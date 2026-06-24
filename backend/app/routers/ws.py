import logging

from fastapi import APIRouter, WebSocket
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from app.chat_service import (
    CharacterNotFoundError,
    ChatBackendError,
    ChatReplySession,
    ChatTimeoutError,
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


async def _receive_json_payload(websocket: WebSocket) -> object:
    try:
        return await websocket.receive_json()
    except ValueError as exc:
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


async def _open_chat_session(
    websocket: WebSocket,
    character_name: str,
) -> ChatReplySession | None:
    try:
        return await websocket.app.state.chat_service.create_chat_session(character_name)
    except CharacterNotFoundError as exc:
        await _send_error(websocket, 404, exc.detail)
        await websocket.close()
        return None


@router.websocket("/ws/{character_name}")
async def websocket_chat(websocket: WebSocket, character_name: str) -> None:
    await websocket.accept()
    logger.info("WebSocket connected for character '%s'", character_name)

    chat_session = await _open_chat_session(websocket, character_name)
    if chat_session is None:
        return

    try:
        while True:
            try:
                payload = await _receive_json_payload(websocket)
                message = _extract_text_message(payload)
            except WebSocketMessageError as exc:
                await _send_error(websocket, 422, str(exc))
                continue

            try:
                reply = await run_in_threadpool(chat_session.generate_reply, message)
            except CharacterNotFoundError as exc:
                await _send_error(websocket, 404, exc.detail)
                await websocket.close()
                return
            except ChatTimeoutError as exc:
                await _send_error(websocket, 504, exc.detail)
                continue
            except ChatBackendError as exc:
                await _send_error(websocket, 502, exc.detail)
                continue

            await websocket.send_json(
                {
                    MESSAGE_TYPE_FIELD: TEXT_MESSAGE_TYPE,
                    RESPONSE_FIELD: reply,
                }
            )
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for character '%s'", character_name)

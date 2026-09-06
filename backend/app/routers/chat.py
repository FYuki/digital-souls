from typing import cast
import asyncio
from contextlib import suppress

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import UUID4, BaseModel, Field

from app.chat_service import (
    CharacterNotFoundError,
    ChatBackendError,
    ChatInputLimitError,
    ChatTimeoutError,
)
from app.routers.validation import ConversationRoute
from app.routers.conversation_contracts import TurnResponse, persisted_turn_response
from app.async_worker import run_sync
from app._chat_runtime import generate_reply_with_tools
from app.tool_use.service import TOOL_STOP_MESSAGE
from app.screen_perception.detector import needs_reference_history
from app.screen_perception.service import (
    ScreenSource,
    ScreenPerceptionError,
    ScreenPerceptionService,
    ScreenTurnMaterial,
)
from app.inference import InferenceCancellationToken
from app.routers.screen_perception import _require_owner

router = APIRouter(route_class=ConversationRoute)


class ChatRequest(BaseModel):
    character: str
    conversation_id: UUID4
    message: str = Field(min_length=1)
    screen_reference: bool = False
    screen_client_session_id: UUID4 | None = None


class PersistedChatResponse(BaseModel):
    character: str
    turn: TurnResponse


@router.post("/chat", response_model=None)
async def chat(
    payload: ChatRequest,
    request: Request,
) -> PersistedChatResponse | JSONResponse:
    if getattr(request.app.state, "tool_service", None) is None:
        return await _chat_response(payload, request)
    owner = asyncio.current_task()
    assert owner is not None
    disconnected = False

    async def watch_disconnect() -> None:
        nonlocal disconnected
        while True:
            if await request.is_disconnected():
                disconnected = True
                request.app.state.tool_service.stop(
                    payload.character, str(payload.conversation_id)
                )
                if not owner.cancelling():
                    owner.cancel("client_disconnected")
                return
            await asyncio.sleep(0.25)

    watcher = asyncio.create_task(watch_disconnect())
    try:
        return await _chat_response(payload, request)
    except asyncio.CancelledError as error:
        request.app.state.tool_service.stop(
            payload.character, str(payload.conversation_id)
        )
        # 明示停止と切断だけをHTTP応答に変換し、shutdown等のcancelは伝搬する。
        if getattr(request.app.state.tool_service, "closing", False) or not (
            error.args == (TOOL_STOP_MESSAGE,)
            or (disconnected and error.args == ("client_disconnected",))
        ):
            raise
        return JSONResponse(
            status_code=499,
            content={
                "detail": "会話応答を停止しました。開始済みの外部操作が取り消されたことは保証されません。"
            },
        )
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher


async def _chat_response(
    payload: ChatRequest, request: Request
) -> PersistedChatResponse | JSONResponse:
    try:
        needs_contextual_reference = needs_reference_history(payload.message)
        if (
            not payload.screen_reference
            and payload.screen_client_session_id is None
            and not needs_contextual_reference
        ):
            reply = await generate_reply_with_tools(
                request.app.state.chat_service.generate_chat_reply,
                payload.character,
                payload.conversation_id,
                payload.message,
            )
            return PersistedChatResponse(
                character=payload.character,
                turn=persisted_turn_response(reply.persisted_turn),
            )
        screen_service = cast(
            ScreenPerceptionService, request.app.state.screen_perception_service
        )
        if payload.screen_client_session_id is not None:
            _require_owner(request, payload.screen_client_session_id)
        history_access = await screen_service.history_access(
            payload.screen_client_session_id
        )
        reference_history = (
            await run_sync(
                request.app.state.chat_service.recent_screen_reference_history,
                payload.character,
                payload.conversation_id,
                history_access,
            )
            if needs_contextual_reference
            else ()
        )
        decision = await screen_service.decide_reference(
            client_session_id=payload.screen_client_session_id,
            question=payload.message,
            explicit_ui=payload.screen_reference,
            history=reference_history,
        )
        if decision.decision == "inspect_screen":
            source: ScreenSource = (
                "explicit_ui" if payload.screen_reference else "natural_language_text"
            )
            if payload.screen_client_session_id is None:
                material = ScreenTurnMaterial(
                    source=source,
                    surface=None,
                    captured_at=None,
                    observation=None,
                    unavailable_reason="session_not_found",
                    request_id=None,
                    validity=InferenceCancellationToken(),
                )
                reply = await generate_reply_with_tools(
                    request.app.state.chat_service.generate_screen_chat_reply,
                    payload.character,
                    payload.conversation_id,
                    payload.message,
                    material,
                    history_access,
                )
            else:
                try:
                    event, _future = await screen_service.begin_request(
                        client_session_id=payload.screen_client_session_id,
                        character_id=payload.character,
                        conversation_id=payload.conversation_id,
                        question=payload.message,
                        source=source,
                        target_hint=decision.target_hint,
                    )
                except ScreenPerceptionError as error:
                    material = ScreenTurnMaterial(
                        source=source,
                        surface=None,
                        captured_at=None,
                        observation=None,
                        unavailable_reason=error.reason_code,
                        request_id=None,
                        validity=InferenceCancellationToken(),
                    )
                    reply = await generate_reply_with_tools(
                        request.app.state.chat_service.generate_screen_chat_reply,
                        payload.character,
                        payload.conversation_id,
                        payload.message,
                        material,
                        history_access,
                    )
                else:
                    return JSONResponse(status_code=202, content=event)
        elif (
            decision.decision == "clarify_reference"
            or decision.unavailable_reason is not None
        ):
            source = (
                "explicit_ui" if payload.screen_reference else "natural_language_text"
            )
            material = ScreenTurnMaterial(
                source=source,
                surface=None,
                captured_at=None,
                observation=None,
                unavailable_reason=(
                    "clarify_reference"
                    if decision.decision == "clarify_reference"
                    else decision.unavailable_reason
                ),
                request_id=None,
                validity=InferenceCancellationToken(),
            )
            reply = await generate_reply_with_tools(
                request.app.state.chat_service.generate_screen_chat_reply,
                payload.character,
                payload.conversation_id,
                payload.message,
                material,
                history_access,
            )
        else:
            reply = await generate_reply_with_tools(
                request.app.state.chat_service.generate_contextual_chat_reply,
                payload.character,
                payload.conversation_id,
                payload.message,
                history_access=history_access,
            )
    except CharacterNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=exc.detail,
        ) from exc
    except ChatInputLimitError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except ChatTimeoutError as exc:
        raise HTTPException(status_code=504, detail=exc.detail) from exc
    except ChatBackendError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc
    return PersistedChatResponse(
        character=payload.character,
        turn=persisted_turn_response(reply.persisted_turn),
    )

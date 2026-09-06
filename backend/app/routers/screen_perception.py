from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response

from app import _chat_runtime
from app.routers.conversation_contracts import persisted_turn_response
from app.screen_perception.generated import (
    SessionHeartbeat,
    SessionRevokeRequested,
    SessionStartRequested,
    ScreenError,
    SnapshotUploadMetadata,
)
from app.screen_perception.http_security import CLIENT_SESSION_COOKIE, ScreenHttpSecurity
from app.screen_perception.service import (
    MAX_IMAGE_BYTES,
    CompletedScreenRequest,
    ScreenPerceptionError,
    ScreenPerceptionService,
)
from app.screen_perception.validation import parse_screen_perception_event


router = APIRouter(prefix="/perception/screen", tags=["screen-perception"])


def _service(request: Request) -> ScreenPerceptionService:
    return cast(ScreenPerceptionService, request.app.state.screen_perception_service)


def _security(request: Request) -> ScreenHttpSecurity:
    return cast(ScreenHttpSecurity, request.app.state.screen_http_security)


def _client_session(request: Request) -> UUID:
    raw = request.cookies.get(CLIENT_SESSION_COOKIE)
    try:
        value = UUID(raw) if raw is not None else None
    except ValueError:
        value = None
    if value is None or value.version != 4:
        raise HTTPException(409, detail={"code": "context_mismatch"})
    return value


def _require_mutation_origin(request: Request) -> None:
    if request.headers.get("origin") != _security(request).allowed_origin:
        raise HTTPException(403, detail={"code": "context_mismatch"})


def _require_owner(request: Request, claimed: UUID) -> None:
    _require_mutation_origin(request)
    if _client_session(request) != claimed:
        raise HTTPException(403, detail={"code": "context_mismatch"})


def _parse_event(value: object, expected: type[object]) -> object:
    try:
        event = parse_screen_perception_event(value)
    except ValueError as error:
        raise HTTPException(422, detail={"code": "context_mismatch"}) from error
    if not isinstance(event, expected):
        raise HTTPException(422, detail={"code": "context_mismatch"})
    return event


def _screen_error(error: ScreenPerceptionError) -> HTTPException:
    return HTTPException(
        error.status_code,
        detail={"code": error.reason_code, "stage": error.stage},
    )


@router.get("/routing")
def routing(request: Request, response: Response) -> dict[str, object]:
    raw = request.cookies.get(CLIENT_SESSION_COOKIE)
    try:
        client_session_id = UUID(raw) if raw is not None else uuid4()
    except ValueError:
        client_session_id = uuid4()
    if client_session_id.version != 4:
        client_session_id = uuid4()
    response.set_cookie(
        CLIENT_SESSION_COOKIE,
        str(client_session_id),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return _service(request).routing_disclosure(client_session_id)


@router.post("/sessions", status_code=201)
async def start_session(body: dict[str, object], request: Request) -> dict[str, object]:
    event = cast(SessionStartRequested, _parse_event(body, SessionStartRequested))
    _require_owner(request, event.client_session_id)
    try:
        return await _service(request).start_session(
            client_session_id=event.client_session_id,
            generation=event.generation,
            character_id=event.character_id,
            conversation_id=event.conversation_id,
            requested_surface=event.requested_surface.value,
            actual_surface=event.actual_surface.value,
            routing_revision=event.routing_revision,
            cloud_vision_consent=event.cloud_consent.cloud_vision,
            cloud_derived_chat_consent=event.cloud_consent.cloud_derived_chat,
        )
    except ScreenPerceptionError as error:
        raise _screen_error(error) from error


@router.post("/sessions/{screen_session_id}/heartbeat")
async def heartbeat(
    screen_session_id: UUID, body: dict[str, object], request: Request
) -> dict[str, object]:
    event = cast(SessionHeartbeat, _parse_event(body, SessionHeartbeat))
    if event.screen_session_id != screen_session_id:
        raise HTTPException(409, detail={"code": "context_mismatch"})
    _require_owner(request, event.client_session_id)
    try:
        return await _service(request).heartbeat(
            screen_session_id=screen_session_id,
            client_session_id=event.client_session_id,
            generation=event.generation,
        )
    except ScreenPerceptionError as error:
        raise _screen_error(error) from error


@router.delete("/sessions/{screen_session_id}")
async def revoke_session(
    screen_session_id: UUID, body: dict[str, object], request: Request
) -> dict[str, object]:
    event = cast(
        SessionRevokeRequested,
        _parse_event(body, SessionRevokeRequested),
    )
    if event.screen_session_id != screen_session_id:
        raise HTTPException(409, detail={"code": "context_mismatch"})
    _require_owner(request, event.client_session_id)
    try:
        return await _service(request).revoke(
            screen_session_id=screen_session_id,
            client_session_id=event.client_session_id,
            generation=event.generation,
            reason=event.reason.value,
        )
    except ScreenPerceptionError as error:
        raise _screen_error(error) from error


@router.put("/requests/{request_id}/image")
async def upload_image(request_id: UUID, request: Request) -> dict[str, object]:
    _require_mutation_origin(request)
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            content_length = int(declared_length)
        except ValueError as error:
            raise HTTPException(422, detail={"code": "image_too_large"}) from error
        if content_length < 1 or content_length > MAX_IMAGE_BYTES:
            raise HTTPException(413, detail={"code": "image_too_large"})
    image = bytearray()
    async for chunk in request.stream():
        image.extend(chunk)
        if len(image) > MAX_IMAGE_BYTES:
            image.clear()
            raise HTTPException(413, detail={"code": "image_too_large"})
    try:
        metadata_value = {
            "protocol_version": request.headers["x-screen-protocol-version"],
            "type": "screen_snapshot_upload_metadata",
            "event_id": request.headers["x-screen-event-id"],
            "screen_session_id": request.headers["x-screen-session-id"],
            "client_session_id": request.headers["x-screen-client-session-id"],
            "generation": int(request.headers["x-screen-generation"]),
            "request_id": str(request_id),
            "turn_id": request.headers["x-screen-turn-id"],
            "image_id": request.headers["x-screen-image-id"],
            "actual_surface": request.headers["x-screen-surface"],
            "captured_at": request.headers["x-screen-captured-at"],
            "mime_type": request.headers["content-type"],
            "width": int(request.headers["x-screen-width"]),
            "height": int(request.headers["x-screen-height"]),
            "byte_length": len(image),
        }
    except (KeyError, ValueError) as error:
        image.clear()
        raise HTTPException(422, detail={"code": "context_mismatch"}) from error
    metadata = cast(
        SnapshotUploadMetadata,
        _parse_event(metadata_value, SnapshotUploadMetadata),
    )
    _require_owner(request, metadata.client_session_id)
    try:
        accepted, completed = await _service(request).accept_image(
            request_id=request_id,
            screen_session_id=metadata.screen_session_id,
            client_session_id=metadata.client_session_id,
            generation=metadata.generation,
            turn_id=metadata.turn_id,
            image_id=metadata.image_id,
            actual_surface=metadata.actual_surface.value,
            captured_at=datetime.fromisoformat(metadata.captured_at),
            mime_type=metadata.mime_type.value,
            width=metadata.width,
            height=metadata.height,
            image_data=bytes(image),
        )
    except ScreenPerceptionError as error:
        raise _screen_error(error) from error
    finally:
        image.clear()
    if completed.material.source == "natural_language_voice":
        return accepted
    return await _complete_text_chat(request, accepted, completed)


@router.post("/requests/{request_id}/failure")
async def report_capture_failure(
    request_id: UUID, body: dict[str, object], request: Request
) -> dict[str, object]:
    _require_mutation_origin(request)
    event = cast(ScreenError, _parse_event(body, ScreenError))
    if (
        event.request_id != request_id
        or event.screen_session_id is None
        or event.turn_id is None
    ):
        raise HTTPException(409, detail={"code": "context_mismatch"})
    client_session_id = _client_session(request)
    try:
        completed = await _service(request).fail_request(
            request_id=request_id,
            screen_session_id=event.screen_session_id,
            client_session_id=client_session_id,
            generation=event.generation,
            turn_id=event.turn_id,
            reason_code=event.reason_code.value,
        )
    except ScreenPerceptionError as error:
        raise _screen_error(error) from error
    if completed.material.source == "natural_language_voice":
        return body
    return await _complete_text_chat(request, body, completed, control_key="error")


async def _complete_text_chat(
    request: Request,
    accepted: dict[str, object],
    completed: CompletedScreenRequest,
    control_key: str = "upload",
) -> dict[str, object]:
    from app._chat_runtime import generate_reply_with_tools

    service = cast(_chat_runtime.ChatService, request.app.state.chat_service)
    history_access = await _service(request).history_access(
        completed.client_session_id
    )
    try:
        reply = await generate_reply_with_tools(
            service.generate_screen_chat_reply,
            completed.character_id,
            completed.conversation_id,
            completed.question,
            completed.material,
            history_access,
        )
    except ScreenPerceptionError as error:
        raise _screen_error(error) from error
    if not completed.material.is_current:
        raise _screen_error(ScreenPerceptionError("request_cancelled", stage="chat"))
    return {
        control_key: accepted,
        "character": completed.character_id,
        "turn": persisted_turn_response(reply.persisted_turn).model_dump(mode="json"),
    }

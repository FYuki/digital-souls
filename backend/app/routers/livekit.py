from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import UUID4, BaseModel, Field

from app.characters.loader import TtsConfigMissingError, TtsConfigValidationError
from app.livekit_transport.bootstrap import (
    BindingValidationError,
    BootstrapService,
    BootstrapConflictError,
    BootstrapTimeoutError,
    UnknownSessionError,
)
from app.routers.screen_perception import _require_owner
from app.tts.irodori_client import IrodoriTtsError
from app.voice_input.models import VadPreparationError


SUPPORTED_PROTOCOL_VERSION = "2.0"
SUPPORTED_TRANSPORT_PROTOCOL_VERSION = "2.0"
MAX_RECONNECT_GRACE_MS = 60_000

router = APIRouter(prefix="/voice/livekit", tags=["livekit"])


class TokenRequest(BaseModel):
    protocol_version: str
    transport_protocol_version: str | None = None
    request_id: UUID
    character_id: str
    conversation_id: UUID
    requested_reconnect_grace_ms: Annotated[int, Field(ge=0, le=MAX_RECONNECT_GRACE_MS)]
    session_id: UUID | None = None
    wait_for_ready: bool = True
    screen_client_session_id: UUID4 | None = None


class TokenResponse(BaseModel):
    session_id: UUID
    participant_id: UUID
    room: str
    token: str
    livekit_url: str
    expires_at: datetime
    reconnect_grace_ms: int


class EndedSessionResponse(BaseModel):
    session_id: UUID
    phase: str


def _configured(request: Request) -> tuple[BootstrapService, str]:
    service = getattr(request.app.state, "livekit_bootstrap_service", None)
    livekit_url = getattr(request.app.state, "livekit_url", None)
    if service is None or not isinstance(livekit_url, str) or not livekit_url.strip():
        raise HTTPException(503, detail={"code": "livekit_not_configured"})
    return cast(BootstrapService, service), livekit_url


def _conflict(code: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code})


@router.post("/token", response_model=TokenResponse)
async def issue_token(body: TokenRequest, request: Request) -> TokenResponse | JSONResponse:
    if body.protocol_version != SUPPORTED_PROTOCOL_VERSION:
        raise HTTPException(
            409,
            detail={
                "code": "protocol_version_mismatch",
                "supported_protocol_version": SUPPORTED_PROTOCOL_VERSION,
            },
        )
    if body.transport_protocol_version != SUPPORTED_TRANSPORT_PROTOCOL_VERSION:
        raise HTTPException(409, detail={
            "code": "transport_protocol_version_mismatch",
            "supported_transport_protocol_version": SUPPORTED_TRANSPORT_PROTOCOL_VERSION,
        })
    service, livekit_url = _configured(request)
    if body.screen_client_session_id is not None:
        _require_owner(request, body.screen_client_session_id)
    raw = body.model_dump(mode="json", exclude_none=True, exclude={"wait_for_ready"})
    try:
        result = (
            await service.bootstrap(raw) if body.wait_for_ready or body.session_id is not None
            else await service.poll_preparation(raw)
        )
        if result is None:
            return JSONResponse(status_code=202, content={
                "status": "preparing", "request_id": str(body.request_id),
            })
    except BindingValidationError as error:
        raise _conflict(error.code) from error
    except BootstrapConflictError as error:
        raise _conflict("bootstrap_conflict") from error
    except UnknownSessionError as error:
        raise _conflict("session_not_reconnectable") from error
    except BootstrapTimeoutError as error:
        raise HTTPException(504, detail={
            "code": "bootstrap_timeout",
            **({"stage": error.stage} if error.stage is not None else {}),
        }) from error
    except (TtsConfigMissingError, TtsConfigValidationError) as error:
        raise HTTPException(503, detail={"code": error.error_code, "stage": "tts"}) from error
    except VadPreparationError as error:
        raise HTTPException(503, detail={"code": "vad_unavailable", "stage": "vad"}) from error
    except IrodoriTtsError as error:
        raise HTTPException(503, detail={"code": error.error_code, "stage": "tts"}) from error
    except Exception as error:
        # 外部URL・認証情報を含み得る例外本文は返さない。
        raise HTTPException(500, detail={"code": "bootstrap_failed"}) from error
    return TokenResponse(
        session_id=UUID(result.session_id),
        participant_id=UUID(result.participant_id),
        room=result.room,
        token=result.token,
        livekit_url=livekit_url,
        expires_at=result.expires_at,
        reconnect_grace_ms=result.reconnect_grace_ms,
    )


@router.post("/preparation/cancel", status_code=204)
async def cancel_preparation(body: TokenRequest, request: Request) -> None:
    service, _ = _configured(request)
    if body.screen_client_session_id is not None:
        _require_owner(request, body.screen_client_session_id)
    raw = body.model_dump(mode="json", exclude_none=True, exclude={"wait_for_ready"})
    try:
        await service.cancel_preparation(raw)
    except BootstrapConflictError as error:
        raise _conflict("bootstrap_conflict") from error


@router.delete("/sessions/{session_id}", response_model=EndedSessionResponse)
async def end_session(session_id: UUID, request: Request) -> EndedSessionResponse:
    service, _ = _configured(request)
    await service.end(str(session_id))
    return EndedSessionResponse(session_id=session_id, phase="ended")

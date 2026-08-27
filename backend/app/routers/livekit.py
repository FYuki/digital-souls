from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.livekit_transport.bootstrap import (
    BindingValidationError,
    BootstrapConflictError,
    BootstrapTimeoutError,
    JOIN_TOKEN_TTL_SECONDS,
    UnknownSessionError,
)


SUPPORTED_PROTOCOL_VERSION = "1.0"
MAX_RECONNECT_GRACE_MS = 60_000

router = APIRouter(prefix="/voice/livekit", tags=["livekit"])


class TokenRequest(BaseModel):
    protocol_version: str
    request_id: UUID
    character_id: str
    conversation_id: UUID
    requested_reconnect_grace_ms: Annotated[int, Field(ge=0)]
    session_id: UUID | None = None


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


def _configured(request: Request) -> tuple[object, str]:
    service = getattr(request.app.state, "livekit_bootstrap_service", None)
    livekit_url = getattr(request.app.state, "livekit_url", None)
    if service is None or not isinstance(livekit_url, str) or not livekit_url.strip():
        raise HTTPException(503, detail={"code": "livekit_not_configured"})
    return service, livekit_url


def _conflict(code: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code})


@router.post("/token", response_model=TokenResponse)
async def issue_token(body: TokenRequest, request: Request) -> TokenResponse:
    if body.protocol_version != SUPPORTED_PROTOCOL_VERSION:
        raise HTTPException(
            409,
            detail={
                "code": "protocol_version_mismatch",
                "supported_protocol_version": SUPPORTED_PROTOCOL_VERSION,
            },
        )
    service, livekit_url = _configured(request)
    raw = body.model_dump(mode="json", exclude_none=True)
    try:
        result = await service.bootstrap(raw)
    except BindingValidationError as error:
        raise _conflict(error.code) from error
    except BootstrapConflictError as error:
        raise _conflict("bootstrap_conflict") from error
    except UnknownSessionError as error:
        raise _conflict("session_not_reconnectable") from error
    except BootstrapTimeoutError as error:
        raise HTTPException(504, detail={"code": "bootstrap_timeout"}) from error
    return TokenResponse(
        session_id=UUID(result.session_id),
        participant_id=UUID(result.participant_id),
        room=result.room,
        token=result.token,
        livekit_url=livekit_url,
        expires_at=datetime.now(UTC) + timedelta(seconds=JOIN_TOKEN_TTL_SECONDS),
        reconnect_grace_ms=result.reconnect_grace_ms,
    )


@router.delete("/sessions/{session_id}", response_model=EndedSessionResponse)
async def end_session(session_id: UUID, request: Request) -> EndedSessionResponse:
    service, _ = _configured(request)
    await service.end(str(session_id))
    return EndedSessionResponse(session_id=session_id, phase="ended")

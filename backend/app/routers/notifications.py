"""通知のローカルユーザー向けAPI。任意参照・ユーザーIDを入力させない。"""
from collections.abc import Callable, Coroutine
import sqlite3
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from app.external_mcp.models import Json, MCPFailure
from app.notifications.runtime import NotificationRuntime
from app.routers.ui_settings import LOCAL_USER_ID


class NotificationRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[object, object, Response]]:
        handler = super().get_route_handler()
        async def safe(request: Request) -> Response:
            try:
                response = await handler(request)
            except MCPFailure as error:
                if error.code == "notification_not_found":
                    raise HTTPException(404, "notification_not_found") from None
                if error.code == "notification_state_conflict":
                    raise HTTPException(409, "notification_state_conflict") from None
                raise HTTPException(403, "notification_unavailable") from None
            except (sqlite3.Error, OSError, TimeoutError):
                raise HTTPException(503, "notification_unavailable") from None
            except RequestValidationError:
                raise HTTPException(422, "invalid_notification_request") from None
            response.headers["Cache-Control"] = "no-store"
            return response
        return safe


router = APIRouter(prefix="/notifications", route_class=NotificationRoute)


def runtime(request: Request) -> NotificationRuntime:
    instance = getattr(request.app.state, "notifications", None)
    if instance is None:
        raise HTTPException(503, "notification_unavailable")
    return cast(NotificationRuntime, instance)


class StatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    state: Literal["read", "unread", "hidden"]
    version: int = Field(ge=1)


class PreferencePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


@router.get("")
async def listing(request: Request, source_id: str | None = None, character_id: str | None = None,
                  unread: bool = False, hidden: bool = False, offset: int = Query(0, ge=0, le=100000),
                  limit: int = Query(50, ge=1, le=100)) -> Json:
    return await runtime(request).listing(LOCAL_USER_ID, source_id=source_id, character_id=character_id,
                                         unread=unread, hidden=hidden, offset=offset, limit=limit)


@router.patch("/preferences/{source_id}/{event_type}", status_code=204)
async def preference(source_id: str, event_type: str, payload: PreferencePatch, request: Request) -> Response:
    await runtime(request).set_preference(LOCAL_USER_ID, source_id, event_type, payload.enabled)
    return Response(status_code=204)


@router.get("/{notification_id}/detail")
async def detail(notification_id: str, request: Request) -> Json:
    return await runtime(request).detail(notification_id, LOCAL_USER_ID)


@router.patch("/{notification_id}")
async def state(notification_id: str, payload: StatePatch, request: Request) -> Json:
    return await runtime(request).set_state(notification_id, LOCAL_USER_ID, payload.state, payload.version)

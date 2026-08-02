from collections.abc import Callable, Coroutine
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BeforeValidator

from app.conversation_history.errors import ConversationNotFoundError

PUBLIC_VALIDATION_ERROR_FIELDS = frozenset({"type", "loc", "msg"})
CONVERSATION_NOT_FOUND_DETAIL = "conversation was not found"


def _canonical_uuid4(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("conversation_id must be a canonical UUIDv4")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("conversation_id must be a canonical UUIDv4") from exc
    if parsed.version != 4 or value != str(parsed):
        raise ValueError("conversation_id must be a canonical UUIDv4")
    return parsed


CanonicalUuid4 = Annotated[UUID, BeforeValidator(_canonical_uuid4)]


class SafeValidationRoute(APIRoute):
    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[object, object, Response]]:
        route_handler = super().get_route_handler()

        async def safe_route_handler(request: Request) -> Response:
            try:
                return await route_handler(request)
            except RequestValidationError as exc:
                detail = [
                    {
                        key: value
                        for key, value in error.items()
                        if key in PUBLIC_VALIDATION_ERROR_FIELDS
                    }
                    for error in exc.errors()
                ]
                raise HTTPException(status_code=422, detail=detail) from exc

        return safe_route_handler


class ConversationRoute(SafeValidationRoute):
    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[object, object, Response]]:
        route_handler = super().get_route_handler()

        async def conversation_route_handler(request: Request) -> Response:
            try:
                return await route_handler(request)
            except ConversationNotFoundError as exc:
                raise HTTPException(
                    status_code=404,
                    detail=CONVERSATION_NOT_FOUND_DETAIL,
                ) from exc

        return conversation_route_handler

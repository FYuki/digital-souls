from collections.abc import Callable, Coroutine

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import UUID4, BaseModel, Field

from app.chat_service import (
    CharacterNotFoundError,
    ChatBackendError,
    ChatInputLimitError,
    ChatTimeoutError,
)

PUBLIC_VALIDATION_ERROR_FIELDS = frozenset({"type", "loc", "msg"})


class _SafeValidationRoute(APIRoute):
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


router = APIRouter(route_class=_SafeValidationRoute)


class ChatRequest(BaseModel):
    character: str
    conversation_id: UUID4
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    character: str
    response: str


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    try:
        reply = request.app.state.chat_service.generate_chat_reply(
            payload.character,
            payload.conversation_id,
            payload.message,
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
    return ChatResponse(character=payload.character, response=reply.response)

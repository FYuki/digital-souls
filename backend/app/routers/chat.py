from fastapi import APIRouter, HTTPException, Request
from pydantic import UUID4, BaseModel, Field

from app.chat_service import (
    CharacterNotFoundError,
    ChatBackendError,
    ChatInputLimitError,
    ChatTimeoutError,
)
from app.routers.validation import ConversationRoute
from app.routers.conversation_contracts import TurnResponse, persisted_turn_response

router = APIRouter(route_class=ConversationRoute)


class ChatRequest(BaseModel):
    character: str
    conversation_id: UUID4
    message: str = Field(min_length=1)


class PersistedChatResponse(BaseModel):
    character: str
    turn: TurnResponse


@router.post("/chat", response_model=PersistedChatResponse)
def chat(
    payload: ChatRequest,
    request: Request,
) -> PersistedChatResponse:
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
    return PersistedChatResponse(
        character=payload.character,
        turn=persisted_turn_response(reply.persisted_turn),
    )

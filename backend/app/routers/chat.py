from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import UUID4, BaseModel, field_validator

from app.chat_service import (
    CharacterNotFoundError,
    ChatBackendError,
    ChatConversationNotFoundError,
    ChatTimeoutError,
)

router = APIRouter()


class ChatRequest(BaseModel):
    character: str
    message: str
    conversation_id: UUID4 | None = None

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class ChatResponse(BaseModel):
    character: str
    conversation_id: UUID
    response: str


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    try:
        reply = request.app.state.chat_service.generate_http_reply(
            payload.character,
            payload.message,
            payload.conversation_id,
        )
    except CharacterNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=exc.detail,
        ) from exc
    except ChatConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail) from exc
    except ChatTimeoutError as exc:
        raise HTTPException(status_code=504, detail=exc.detail) from exc
    except ChatBackendError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc
    return ChatResponse(
        character=payload.character,
        conversation_id=reply.conversation_id,
        response=reply.response,
    )

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.chat_service import (
    CharacterNotFoundError,
    ChatBackendError,
    ChatTimeoutError,
    generate_chat_reply,
)

router = APIRouter()


class ChatRequest(BaseModel):
    character: str
    message: str


class ChatResponse(BaseModel):
    character: str
    response: str


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    try:
        reply = generate_chat_reply(
            request.character,
            request.message,
        )
    except CharacterNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=exc.detail,
        ) from exc
    except ChatTimeoutError as exc:
        raise HTTPException(status_code=504, detail=exc.detail) from exc
    except ChatBackendError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc
    return ChatResponse(character=request.character, response=reply)

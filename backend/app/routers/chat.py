from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.chat_service import (
    CharacterNotFoundError,
    ChatBackendError,
    ChatInputLimitError,
    ChatTimeoutError,
)

router = APIRouter()


class ChatRequest(BaseModel):
    character: str
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    character: str
    response: str


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    try:
        reply = request.app.state.chat_service.generate_chat_reply(
            payload.character,
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
    return ChatResponse(character=payload.character, response=reply)

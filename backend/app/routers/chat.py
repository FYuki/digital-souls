import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.characters.loader import load_personality
from app.llm.router import generate_response

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
        system_prompt = load_personality(request.character)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Character '{request.character}' not found")

    try:
        reply = generate_response(system_prompt, request.message)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="LLM request timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="LLM request failed") from exc
    return ChatResponse(character=request.character, response=reply)

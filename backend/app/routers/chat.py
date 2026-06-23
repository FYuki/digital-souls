import os

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from app.characters.loader import load_personality
from app.llm.router import generate_response
from app.memory.memory_policy import resolved_memory_policy
from app.memory.rag_service import build_augmented_system_prompt, record_chat_turn

router = APIRouter()
RAG_ENABLED_ENV = "RAG_ENABLED"
RAG_ENABLED_VALUE = "true"


class ChatRequest(BaseModel):
    character: str
    message: str


class ChatResponse(BaseModel):
    character: str
    response: str


def _rag_enabled() -> bool:
    return os.environ.get(RAG_ENABLED_ENV) == RAG_ENABLED_VALUE


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, background_tasks: BackgroundTasks) -> ChatResponse:
    rag_enabled = _rag_enabled()
    try:
        system_prompt = load_personality(request.character)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Character '{request.character}' not found",
        )

    augmented_prompt = system_prompt
    if rag_enabled:
        memory_policy = resolved_memory_policy()
        augmented_prompt = build_augmented_system_prompt(
            request.character,
            request.message,
            system_prompt,
            memory_policy,
        )
    try:
        reply = generate_response(augmented_prompt, request.message)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="LLM request timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="LLM request failed") from exc
    if rag_enabled:
        record_chat_turn(
            request.character,
            request.message,
            reply,
            background_tasks,
            memory_policy,
        )
    return ChatResponse(character=request.character, response=reply)

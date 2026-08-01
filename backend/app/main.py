from contextlib import ExitStack, asynccontextmanager
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI

from app import _chat_runtime
from app.audio_pipeline import (
    create_audio_pipeline_service,
    resolve_audio_runtime_config,
)
from app.chat_prompt import build_chat_prompt
from app.characters.loader import load_character_card
from app.conversation_history.config import resolve_conversation_history_config
from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.schema import initialize_conversation_history_schema
from app.conversation_history.service import ConversationHistoryService
from app.llm.router import count_input_tokens, generate_response
from app.memory.memory_policy import resolved_memory_policy
from app.prompting import BuiltPrompt, CharacterPrompt, PromptMessage
from app.privacy.history_sanitizer import create_history_sanitizer
from app.privacy.scanner import create_privacy_scanner
from app.routers.chat import router as chat_router
from app.routers.ws import router as ws_router

load_dotenv()

RAG_MEMORY_WORKERS = _chat_runtime.DEFAULT_RAG_MEMORY_WORKERS


def _load_character_prompt(character: str) -> CharacterPrompt:
    return load_character_card(character).to_character_prompt()


def _generate_llm_response(
    prompt: BuiltPrompt,
    *,
    max_output_tokens: int,
) -> str:
    return generate_response(prompt, max_output_tokens=max_output_tokens)


def _count_llm_input_tokens(messages: tuple[PromptMessage, ...]) -> int:
    return count_input_tokens(messages)


def _app_chat_service(app: FastAPI) -> _chat_runtime.ChatService:
    return cast(_chat_runtime.ChatService, app.state.chat_service)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    policy = resolved_memory_policy()
    privacy_scanner = create_privacy_scanner(policy.privacy)
    history_sanitizer = create_history_sanitizer(privacy_scanner, policy.privacy)
    conversation_history_config = resolve_conversation_history_config()
    initialize_conversation_history_schema(
        conversation_history_config.database_path,
    )
    conversation_history_repository = ConversationHistoryRepository(
        database_path=conversation_history_config.database_path,
        stale_after=conversation_history_config.stale_after,
        retention=conversation_history_config.retention,
        clock=lambda: datetime.now(UTC),
        uuid_factory=uuid4,
    )
    conversation_history_repository.recover_stale_processing()
    executor = None
    memory_task_queue = None
    chat_service_resolver = None
    repository_state_set = False
    resolver_registered = False
    chat_service_state_set = False
    audio_pipeline_state_set = False
    try:
        app.state.conversation_history_repository = conversation_history_repository
        repository_state_set = True
        executor = ThreadPoolExecutor(
            max_workers=RAG_MEMORY_WORKERS,
            thread_name_prefix=_chat_runtime.RAG_MEMORY_THREAD_PREFIX,
        )
        memory_task_queue = _chat_runtime.create_thread_pool_memory_task_queue(executor)
        app_chat_service = _chat_runtime.create_chat_service(
            _chat_runtime.resolve_chat_runtime_config(policy, privacy_scanner),
            memory_task_queue,
            ConversationHistoryService(
                conversation_history_repository,
                history_sanitizer,
            ),
            _chat_runtime.ChatRuntimeDependencies(
                character_prompt_loader=_load_character_prompt,
                prompt_builder=build_chat_prompt,
                llm_response_generator=_generate_llm_response,
                input_token_counter=_count_llm_input_tokens,
            ),
        )
        app.state.chat_service = app_chat_service
        chat_service_state_set = True
        app.state.audio_pipeline_service = create_audio_pipeline_service(
            resolve_audio_runtime_config(),
        )
        audio_pipeline_state_set = True
        chat_service_resolver = lambda: _app_chat_service(app)
        _chat_runtime.register_default_chat_service_resolver(chat_service_resolver)
        resolver_registered = True
        yield
    finally:
        with ExitStack() as cleanup:
            if memory_task_queue is not None:
                cleanup.callback(memory_task_queue.shutdown)
            elif executor is not None:
                cleanup.callback(executor.shutdown, wait=True)
            if chat_service_state_set:
                cleanup.callback(delattr, app.state, "chat_service")
            if audio_pipeline_state_set:
                cleanup.callback(delattr, app.state, "audio_pipeline_service")
                cleanup.callback(app.state.audio_pipeline_service.close)
            if resolver_registered and chat_service_resolver is not None:
                cleanup.callback(
                    _chat_runtime.clear_default_chat_service_resolver,
                    chat_service_resolver,
                )
            if repository_state_set:
                cleanup.callback(
                    delattr,
                    app.state,
                    "conversation_history_repository",
                )


app = FastAPI(lifespan=lifespan)

app.include_router(chat_router)
app.include_router(ws_router)


@app.get("/")
def health_check() -> dict[str, str]:
    return {"status": "ok"}

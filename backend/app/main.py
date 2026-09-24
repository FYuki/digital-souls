from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from uuid import UUID

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import _chat_runtime
from app.async_worker import run_sync
from app.addon_action.interaction import confirmation_resume_scope
from app.characters.loader import load_character_card
from app.chat_prompt import build_chat_prompt
from app.conversation_core.control_input import current_control_request
from app.conversation_history.models import ConversationTurn, TurnStatus
from app.conversation_history.service import HistorySession
from app.inference import InferenceCaller, InferenceTarget
from app.inference.config import INFERENCE_TARGET_PREFIX, reject_legacy_inference_environment
from app.notifications.startup import NotificationAvailabilityMiddleware, notification_lifespan
from app.llm import router as llm_router
from app.memory.formation.contracts import MemoryFormationJob
from app.model_settings import ModelSettings
from app.prompting import BuiltPrompt
from app.routers.addon_actions import router as addon_actions_router
from app.routers.addon_admin import router as addon_admin_router
from app.routers.character_catalog import router as character_catalog_router
from app.routers.character_life import router as character_life_router
from app.routers.chat import router as chat_router
from app.routers.conversations import router as conversations_router
from app.routers.episodic_memories import router as episodic_memories_router
from app.routers.livekit import router as livekit_router
from app.routers.notifications import router as notifications_router
from app.routers.memory_management import router as memory_management_router
from app.routers.screen_perception import router as screen_perception_router
from app.routers.semantic_memories import router as semantic_memories_router
from app.routers.tool_use import router as tool_use_router
from app.routers.ui_settings import router as ui_settings_router
from app.routers.ws import router as ws_router
from app.runtime.application import ApplicationRuntime, LifespanWiring, NotificationStartupFallback
from app.screen_perception.detector import needs_reference_history
from app.screen_perception.provenance import ScreenLineage
from app.screen_perception.service import (
    ScreenHistoryAccess,
    ScreenPerceptionError,
    ScreenTurnMaterial,
)
from app.tool_use.service import ToolService

if TYPE_CHECKING:
    from app.livekit_transport.core_factory import (
        ProductionConversationCoreSessionFactory,
    )

load_dotenv()


def _load_character_definition(
    character: str,
) -> _chat_runtime.CharacterRuntimeDefinition:
    card = load_character_card(character)
    return _chat_runtime.CharacterRuntimeDefinition(
        prompt=card.to_character_prompt(),
        character_book=card.data.character_book,
    )


def _validate_screen_context(
    runtime: ApplicationRuntime, character_id: str, conversation_id: UUID
) -> None:
    load_character_card(character_id)
    history = runtime.history
    assert history is not None and history.repository is not None
    history.repository.resume_conversation(character_id, conversation_id)


def _episodic_entity_labels(character_id: str) -> dict[str, str]:
    card = load_character_card(character_id)
    return {
        "speaker:user": "ユーザー",
        f"character:{character_id}": card.data.name,
    }


def _create_app_chat_service(
    runtime: ApplicationRuntime,
) -> _chat_runtime.ChatService:
    privacy = runtime.privacy
    history = runtime.history
    memory = runtime.memory
    model_settings = runtime.model_settings
    runtime_paths = runtime.runtime_paths
    clock = runtime.clock
    assert (
        privacy is not None
        and privacy.scanner is not None
        and privacy.classifier is not None
        and history is not None
        and history.service is not None
        and model_settings is not None
        and runtime_paths is not None
        and clock is not None
        and memory.read_repository is not None
        and memory.embedder is not None
        and memory.formation_scheduler is not None
        and memory.provenance_recorder is not None
    )
    return _chat_runtime.create_chat_service(
        _chat_runtime.resolve_chat_runtime_config(
            privacy.policy,
            model_settings,
            runtime_paths,
            runtime.occurred_timezone,
        ),
        history.service,
        _chat_runtime.ChatRuntimeDependencies(
            character_definition_loader=_load_character_definition,
            prompt_builder=build_chat_prompt,
            llm_response_generator=runtime.generate_llm_response,
            input_token_counter=runtime.count_llm_input_tokens,
            privacy_scanner=privacy.scanner,
            semantic_classifier=privacy.classifier,
            approved_memory_repository=memory.read_repository,
            memory_embedder=memory.embedder,
            memory_formation_submitter=memory.formation_scheduler,
            clock=clock,
            tools=runtime.tools.routing_service,
            life_context=runtime.life.context,
            response_provenance_recorder=memory.provenance_recorder.record,
            response_history_filter=memory.provenance_recorder.filter_history,
        ),
    )


def _create_core_session_factory(
    runtime: ApplicationRuntime,
) -> ProductionConversationCoreSessionFactory:
    from app.livekit_transport.core_factory import (
        ProductionConversationCoreSessionFactory,
    )

    app = runtime.app
    app_chat_service = runtime.chat.service
    model_settings = runtime.model_settings
    inference = runtime.inference
    audio = runtime.audio
    history = runtime.history
    formation_scheduler = runtime.memory.formation_scheduler
    history_repository = history.repository if history is not None else None
    assert (
        app_chat_service is not None
        and model_settings is not None
        and inference is not None
        and audio is not None
        and history is not None
        and history.service is not None
        and history_repository is not None
        and formation_scheduler is not None
        and audio.core_transcriber is not None
        and audio.core_synthesizer is not None
    )

    async def generate_screen_core_reply_stream(
        session_id: str,
        client_session_id: UUID | None,
        character: str,
        conversation_id: UUID,
        history_session: object,
        transcript: str,
        screen_lineage_observer: Callable[
            [tuple[ScreenLineage, ...]], None
        ],
        prompt_observer: Callable[[BuiltPrompt], None] | None,
    ) -> AsyncIterator[str]:
        history_access = (
            await app.state.screen_perception_service.history_access(
                client_session_id
            )
        )
        reference_history = (
            await run_sync(
                app_chat_service.recent_screen_reference_history,
                character,
                conversation_id,
                history_access,
            )
            if needs_reference_history(transcript)
            else ()
        )
        screen_material = (
            await app.state.screen_perception_service.await_voice_material(
                client_session_id=client_session_id,
                character_id=character,
                conversation_id=conversation_id,
                question=transcript,
                publish_request=lambda payload: (
                    app.state.livekit_runtime_manager.send_screen(
                        session_id, payload
                    )
                ),
                history=reference_history,
            )
        )
        async for text in _stream_core_reply(
            app_chat_service,
            model_settings,
            character,
            history_session,  # type: ignore[arg-type]
            transcript,
            screen_material,
            history_access,
            screen_lineage_observer,
            tools=app.state.tool_service,
            conversation_id=str(conversation_id),
            prompt_observer=prompt_observer,
        ):
            yield text

    def submit_completed_core_turn(persisted_turn: object) -> None:
        if not isinstance(persisted_turn, ConversationTurn):
            raise TypeError(
                "completed Core turn must be a ConversationTurn"
            )
        if persisted_turn.status is not TurnStatus.COMPLETED:
            return
        if history_repository.is_screen_derived(
            persisted_turn.character_id,
            persisted_turn.conversation_id,
            persisted_turn.turn_id,
        ):
            return
        formation_scheduler.submit(
            MemoryFormationJob(
                character_id=persisted_turn.character_id,
                conversation_id=persisted_turn.conversation_id,
                turn_id=persisted_turn.turn_id,
            )
        )

    return ProductionConversationCoreSessionFactory(
        transcriber=audio.core_transcriber,
        synthesizer=audio.core_synthesizer,
        history_service=history.service,
        completed_turn_observer=submit_completed_core_turn,
        response_provenance_recorder=app_chat_service.record_response_provenance,
        generate_screen_reply_stream=generate_screen_core_reply_stream,
        prepare_prompt=lambda character: (
            app_chat_service.prepare_character_input_tokens(
                character,
                timeout_seconds=inference.runtime.settings.target(
                    InferenceTarget.CHAT
                ).timeout_seconds,
            )
        ),
        prepare_inference=lambda: inference.runtime.router.prepare_text(
            caller=InferenceCaller.CHAT,
            target=InferenceTarget.CHAT,
            latency_sensitive=True,
        ),
        on_conversation_interruption=(
            runtime.tools.runtime.service.interrupted
            if runtime.tools.runtime is not None
            else lambda _character, _conversation, _reason: None
        ),
        measurement_kind=audio.measurement_kind,
        trace_record=(
            audio.trace_recorder.record
            if audio.trace_recorder is not None
            else None
        ),
    )


_WIRING = LifespanWiring(
    create_chat_service=_create_app_chat_service,
    create_core_session_factory=_create_core_session_factory,
    validate_screen_context=_validate_screen_context,
    episodic_entity_labels=_episodic_entity_labels,
)


async def _stream_core_reply(
    chat_service: _chat_runtime.ChatService,
    model_settings: ModelSettings,
    character: str,
    history_session: HistorySession,
    transcript: str,
    screen: ScreenTurnMaterial | None = None,
    history_access: ScreenHistoryAccess | None = None,
    screen_lineage_observer: Callable[[tuple[ScreenLineage, ...]], None] | None = None,
    tools: ToolService | None = None,
    conversation_id: str | None = None,
    prompt_observer: Callable[[BuiltPrompt], None] | None = None,
) -> AsyncIterator[str]:
    from app.inference.diagnostics import diagnostic

    diagnostic("prompt_preparation_started")
    prepared = await chat_service.prepare_reply(
        character,
        transcript,
        history_session,
        conversation=conversation_id,
        screen=screen,
        history_access=history_access,
        tools=tools,
        base_prompt_observer=(
            None
            if screen_lineage_observer is None
            else lambda prompt: screen_lineage_observer(prompt.screen_lineages)
        ),
        tool_scope=(
            confirmation_resume_scope(
                character, conversation_id, current_control_request()
            )
            if tools is not None and conversation_id is not None
            else None
        ),
    )
    prompt = prepared.prompt
    if prompt_observer is not None:
        prompt_observer(prompt)
    if prepared.direct_reply is not None:
        yield prepared.direct_reply
        return
    # ツール結果と生活状態を反映した、生成へ渡す最終promptを計測する。
    diagnostic("prompt_preparation_completed")
    diagnostic("prompt_message_count", len(prompt.messages))
    diagnostic("prompt_input_tokens", prompt.usage.total)
    for part in ("character", "character_lore", "history", "rag", "current_user", "post_history"):
        diagnostic(f"prompt_{part}_tokens", getattr(prompt.usage, part))
    if history_access is not None and not all(
        history_access.allows(lineage) for lineage in prompt.screen_lineages
    ):
        raise ScreenPerceptionError("request_cancelled", stage="chat")
    async for text in llm_router.stream_response(
        prompt,
        max_output_tokens=prepared.max_output_tokens,
        settings=model_settings,
        latency_sensitive=True,
    ):
        if screen is not None and not screen.is_current:
            raise ScreenPerceptionError("request_cancelled", stage="chat")
        if history_access is not None and not all(
            history_access.allows(lineage) for lineage in prompt.screen_lineages
        ):
            raise ScreenPerceptionError("request_cancelled", stage="chat")
        yield text
    if screen is not None and not screen.is_current:
        raise ScreenPerceptionError("request_cancelled", stage="chat")
    chat_service.record_successful_prompt_references(prompt)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    reject_legacy_inference_environment(os.environ)
    app.state.notification_only = False
    configured = bool(os.environ.get("DS_NOTIFICATION_CONFIG"))
    if configured and not any(key.startswith(INFERENCE_TARGET_PREFIX) for key in os.environ):
        async with notification_lifespan(app):
            yield
        return
    # yield後の例外で二度起動せず、必須推論先の起動probe失敗だけ縮退する。
    try:
        async with _conversation_lifespan(app):
            yield
    except NotificationStartupFallback:
        async with notification_lifespan(app):
            yield


@asynccontextmanager
async def _conversation_lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = ApplicationRuntime(app, os.environ)
    try:
        runtime.prepare()
        with runtime.acquire_persistent_state():
            try:
                await runtime.start(_WIRING)
                yield
            finally:
                await runtime.shutdown()
    except BaseException:
        # prepare/acquireの失敗ではshutdownに到達しないため、
        # 構築済みの資源だけをここで回収する（冪等）。
        runtime.abort()
        raise


app = FastAPI(lifespan=lifespan)
app.add_middleware(NotificationAvailabilityMiddleware)
app.include_router(tool_use_router)
app.include_router(addon_actions_router)
app.include_router(character_life_router)
app.include_router(addon_admin_router)
app.include_router(notifications_router)

app.include_router(chat_router)
app.include_router(character_catalog_router)
app.include_router(conversations_router)
app.include_router(memory_management_router)
app.include_router(episodic_memories_router)
app.include_router(semantic_memories_router)
app.include_router(ui_settings_router)
app.include_router(livekit_router)
app.include_router(screen_perception_router)
app.include_router(ws_router)


@app.get("/")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def inference_readiness() -> JSONResponse:
    if getattr(app.state, "notification_only", False):
        return JSONResponse({"status": "ready", "mode": "notifications_only"})
    health = getattr(app.state, "inference_health", None)
    ready = health is not None and health.is_ready()
    return JSONResponse(
        {"status": "ready" if ready else "not_ready"},
        status_code=200 if ready else 503,
    )


@app.get("/health/inference")
def inference_health() -> JSONResponse:
    health = getattr(app.state, "inference_health", None)
    if health is None:
        return JSONResponse({"targets": []}, status_code=503)
    return JSONResponse(
        {"targets": [item.public_dict() for item in health.snapshot()]},
        status_code=200,
    )

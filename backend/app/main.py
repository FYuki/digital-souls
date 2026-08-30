from contextlib import ExitStack, asynccontextmanager
import logging
import os
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
import sqlite3
from pathlib import Path
from typing import cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI

from app import _chat_runtime
from app.audio_pipeline import (
    create_audio_pipeline_service,
    resolve_audio_runtime_config,
)
from app.chat_prompt import build_chat_prompt
from app.characters.loader import load_character_card
from app.backup_restore import (
    BackupAuthenticationKey,
    create_backup,
    resolve_backup_authentication_key,
    restore_backup,
    verify_backup,
    verify_restored_backup,
)
from app.conversation_history.config import resolve_conversation_history_config
from app.conversation_history.errors import SchemaRollbackError
from app.conversation_history.lifecycle_service import ConversationLifecycleService
from app.conversation_history.models import ConversationTurn, TurnStatus
from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.schema import (
    initialize_conversation_history_schema,
    inspect_conversation_history_schema,
)
from app.conversation_history.service import ConversationHistoryService
from app.conversation_history.sqlite_lease import SQLiteLease, acquire_maintenance_lease
from app.conversation_history.wal_cleanup import ConversationWalCleanup
from app.environment import iana_timezone_environment_value
from app.llm import router as llm_router
from app.memory.memory_policy import resolved_memory_policy
from app.memory.admission.evaluator import create_rag_admission_evaluator
from app.memory.admission_service import RagAdmissionService
from app.memory.embedder import embed_text
from app.memory.index_scheduler import MemoryIndexScheduler
from app.memory.index_sync import MemoryIndexSync
from app.memory.consolidation.config import resolve_memory_consolidation_settings
from app.memory.consolidation.local_llm import require_local_ollama_base_url
from app.memory.consolidation.planner import ConsolidationPlanner
from app.memory.consolidation.privacy import ConsolidationPrivacyReviewer
from app.memory.consolidation.scheduler import (
    ConsolidationPriorityProbe,
    MemoryConsolidationScheduler,
    is_consolidation_eligible,
)
from app.memory.consolidation.service import MemoryConsolidationService
from app.memory.formation.config import resolve_memory_formation_settings
from app.memory.formation.contracts import MemoryFormationJob
from app.memory.formation.extractor import EXTRACTOR_VERSION, MemoryCandidateExtractor
from app.memory.formation.ollama_client import OllamaMemoryExtractorClient
from app.llm.ollama_config import resolve_ollama_base_url
from app.memory.formation.scheduler import MemoryFormationScheduler
from app.memory.formation.worker import MemoryFormationWorker
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.index_outbox_repository import IndexOutboxRepository
from app.memory.persistence.temporary_repository import TemporaryProviderRecordRepository
from app.memory.providers import AddonRecordProvider, PersonaMemoryProvider
from app.model_settings import resolve_model_settings
from app.prompting import BuiltPrompt, CharacterPrompt, PromptMessage
from app.privacy.history_sanitizer import create_history_sanitizer
from app.privacy.scanner import create_privacy_scanner
from app.privacy.semantic.classifier import OllamaSemanticPrivacyClassifier
from app.privacy.semantic.ollama_classifier_client import OllamaClassifierClient
from app.routers.chat import router as chat_router
from app.routers.conversations import router as conversations_router
from app.routers.memory_management import router as memory_management_router
from app.routers.livekit import router as livekit_router
from app.routers.ws import router as ws_router
from app.runtime_data_root import (
    initialize_runtime_data_root,
    remove_legacy_chroma_index_once,
)
from app.runtime_paths import (
    RuntimePaths,
    resolve_runtime_paths,
    runtime_paths_projection,
)
from app.voice_metrics import (
    JsonlTraceRecorder,
    MeasurementKind,
    cleanup_expired_raw_traces,
    resolve_raw_trace_root,
)

VOICE_MEASUREMENT_KIND_ENV = "VOICE_MEASUREMENT_KIND"
VOICE_CONTROLLED_TRACE_PATH_ENV = "VOICE_CONTROLLED_TRACE_PATH"

load_dotenv()

MEMORY_OCCURRED_TIMEZONE_ENV = "MEMORY_OCCURRED_TIMEZONE"
DEFAULT_MEMORY_OCCURRED_TIMEZONE = "Asia/Tokyo"
DOGFOOD_BACKUP_DIR_ENV = "DOGFOOD_BACKUP_DIR"
DOGFOOD_BACKUP_RETENTION_COUNT_ENV = "DOGFOOD_BACKUP_RETENTION_COUNT"
CONSOLIDATION_PROMPT_VERSION = "consolidation-v1"


@dataclass(frozen=True)
class _SchemaRollbackContext:
    generation: Path
    authentication_key: BackupAuthenticationKey


def ensure_schema_backup_gate(
    paths: RuntimePaths, repository_root: Path
) -> _SchemaRollbackContext | None:
    inspection = inspect_conversation_history_schema(paths.sqlite_path)
    if paths.environment_id != "dogfood" or not inspection.migration_required:
        return None
    backup_root_value = os.environ.get(DOGFOOD_BACKUP_DIR_ENV)
    retention_value = os.environ.get(DOGFOOD_BACKUP_RETENTION_COUNT_ENV)
    if not backup_root_value or retention_value is None:
        raise RuntimeError("dogfood schema backup configuration is required")
    try:
        retention_count = int(retention_value)
    except ValueError as error:
        raise RuntimeError("dogfood backup retention count is invalid") from error
    if retention_count <= 0:
        raise RuntimeError("dogfood backup retention count is invalid")
    authentication_key = resolve_backup_authentication_key(os.environ)
    generation = create_backup(
        runtime_paths=paths,
        repository_root=repository_root,
        backup_root=Path(backup_root_value),
        retention_count=retention_count,
        authentication_key=authentication_key,
    )
    verify_backup(
        backup_directory=generation,
        authentication_key=authentication_key,
    )
    return _SchemaRollbackContext(generation, authentication_key)


def _initialize_schema_with_rollback(
    *,
    database_path: Path,
    runtime_paths: RuntimePaths,
    repository_root: Path,
    rollback: _SchemaRollbackContext | None,
    maintenance_lease: SQLiteLease | None = None,
) -> None:
    if rollback is None:
        initialize_conversation_history_schema(database_path)
        return
    try:
        initialize_conversation_history_schema(database_path)
    except Exception as primary_error:
        try:
            restore_backup(
                runtime_paths=runtime_paths,
                repository_root=repository_root,
                backup_directory=rollback.generation,
                authentication_key=rollback.authentication_key,
                maintenance_lease=maintenance_lease,
            )
        except Exception as compensation_error:
            raise SchemaRollbackError(
                primary_error, compensation_error, "restore"
            ) from None
        try:
            verify_restored_backup(
                runtime_paths=runtime_paths,
                repository_root=repository_root,
                backup_directory=rollback.generation,
                authentication_key=rollback.authentication_key,
            )
        except Exception as compensation_error:
            raise SchemaRollbackError(
                primary_error, compensation_error, "verification"
            ) from None
        raise


def _load_character_prompt(character: str) -> CharacterPrompt:
    return load_character_card(character).to_character_prompt()


def _app_chat_service(app: FastAPI) -> _chat_runtime.ChatService:
    return cast(_chat_runtime.ChatService, app.state.chat_service)


def log_runtime_configuration(paths: RuntimePaths) -> None:
    logging.getLogger(__name__).info(
        "Runtime configuration: %s", runtime_paths_projection(paths)
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.livekit_transport.production import (
        ProductionConversationCoreSessionFactory,
        configure_production_resources,
        resolve_livekit_settings,
    )

    livekit_api = None
    model_settings = resolve_model_settings(os.environ)
    occurred_timezone = iana_timezone_environment_value(
        MEMORY_OCCURRED_TIMEZONE_ENV,
        DEFAULT_MEMORY_OCCURRED_TIMEZONE,
    )
    repository_root = Path(__file__).resolve().parents[2]
    runtime_paths = resolve_runtime_paths(os.environ, repository_root)
    initialize_runtime_data_root(runtime_paths, repository_root)
    voice_trace_recorder = None
    voice_measurement_kind: MeasurementKind = "automated_test"
    configured_measurement_kind = os.environ.get(VOICE_MEASUREMENT_KIND_ENV)
    if configured_measurement_kind is not None:
        if configured_measurement_kind != "controlled_baseline":
            raise ValueError("VOICE_MEASUREMENT_KIND must be controlled_baseline")
        controlled_trace_path = os.environ.get(VOICE_CONTROLLED_TRACE_PATH_ENV)
        if controlled_trace_path is None:
            raise ValueError("VOICE_CONTROLLED_TRACE_PATH is required")
        trace_path = Path(controlled_trace_path).resolve()
        data_root = runtime_paths.data_root.resolve()
        if data_root != trace_path.parent and data_root not in trace_path.parents:
            raise ValueError("controlled trace must be inside the controlled data root")
        voice_trace_recorder = JsonlTraceRecorder(trace_path)
        voice_measurement_kind = "controlled_baseline"
    elif runtime_paths.environment_id == "dogfood":
        raw_trace_root = resolve_raw_trace_root(
            repository_root=repository_root,
            data_root=runtime_paths.data_root,
            measurement_kind="dogfood",
        )
        raw_trace_root.mkdir(parents=True, exist_ok=True)
        cleanup_expired_raw_traces(raw_trace_root, now=datetime.now(UTC))
        voice_trace_recorder = JsonlTraceRecorder(
            raw_trace_root / f"{uuid4()}.jsonl"
        )
        voice_measurement_kind = "dogfood"
    from app.restore_intent import require_no_restore_intent

    require_no_restore_intent(runtime_paths.restore_intent_path)
    policy = resolved_memory_policy()
    remove_legacy_chroma_index_once(runtime_paths, repository_root)
    log_runtime_configuration(runtime_paths)

    def generate_llm_response(
        prompt: BuiltPrompt, *, max_output_tokens: int
    ) -> str:
        return llm_router.generate_response(
            prompt,
            max_output_tokens=max_output_tokens,
            settings=model_settings,
        )

    def count_llm_input_tokens(messages: tuple[PromptMessage, ...]) -> int:
        return llm_router.count_input_tokens(messages, settings=model_settings)

    privacy_scanner = create_privacy_scanner(policy.privacy)
    history_sanitizer = create_history_sanitizer(privacy_scanner, policy.privacy)
    conversation_history_config = resolve_conversation_history_config(runtime_paths)
    with acquire_maintenance_lease(conversation_history_config.database_path) as lease:
        rollback = ensure_schema_backup_gate(runtime_paths, repository_root)
        _initialize_schema_with_rollback(
            database_path=conversation_history_config.database_path,
            runtime_paths=runtime_paths,
            repository_root=repository_root,
            rollback=rollback,
            maintenance_lease=lease,
        )
        lease.transition_to_runtime()
        from app.memory.persistence.schema import initialize_persona_memory_schema

        initialize_persona_memory_schema(runtime_paths, repository_root)
        def clock() -> datetime:
            return datetime.now(UTC)
        wal_cleanup = ConversationWalCleanup(
            database_path=conversation_history_config.database_path,
            clock=clock,
            connection_factory=sqlite3.connect,
        )
        conversation_history_repository = ConversationHistoryRepository(
            database_path=conversation_history_config.database_path,
            stale_after=conversation_history_config.stale_after,
            retention=conversation_history_config.retention,
            clock=clock,
            uuid_factory=uuid4,
            wal_cleanup=wal_cleanup,
        )
        conversation_history_repository.recover_stale_processing()
        wal_cleanup.retry_pending()
        conversation_lifecycle_service = ConversationLifecycleService(
            conversation_history_repository
        )
        approved_memory_repository = ApprovedMemoryRepository(
            database_path=runtime_paths.persona_memory_sqlite_path,
            clock=clock,
            uuid_factory=uuid4,
            outbox_uuid_factory=uuid4,
        )
        outbox_repository = IndexOutboxRepository(
            database_path=runtime_paths.persona_memory_sqlite_path,
            clock=clock,
        )
        memory_index_sync = MemoryIndexSync(
            approved_repository=approved_memory_repository,
            outbox_repository=outbox_repository,
            chroma_path=runtime_paths.chroma_path,
            runtime_report_dir=runtime_paths.runtime_report_dir,
            embedder=embed_text,
            clock=clock,
        )
        temporary_record_repository = TemporaryProviderRecordRepository(
            database_path=runtime_paths.persona_memory_sqlite_path,
            clock=clock,
            uuid_factory=uuid4,
        )
        memory_index_scheduler = MemoryIndexScheduler(memory_index_sync)
        formation_settings = resolve_memory_formation_settings(os.environ)
        consolidation_settings = resolve_memory_consolidation_settings(os.environ)
        chat_service_resolver = None
        repository_state_set = False
        lifecycle_service_state_set = False
        resolver_registered = False
        chat_service_state_set = False
        audio_pipeline_state_set = False
        semantic_classifier_state_set = False
        persona_memory_provider_state_set = False
        addon_record_provider_state_set = False
        rag_admission_service_state_set = False
        voice_trace_recorder_state_set = False
        voice_measurement_kind_state_set = False
        semantic_classifier_client = None
        memory_index_scheduler_started = False
        memory_formation_scheduler_started = False
        memory_consolidation_scheduler_started = False
        memory_extractor_client = None
        memory_consolidation_client = None
        memory_consolidation_classifier_client = None
        core_transcriber = None
        core_synthesizer = None
        try:
            app.state.voice_measurement_kind = voice_measurement_kind
            voice_measurement_kind_state_set = True
            if voice_trace_recorder is not None:
                app.state.voice_trace_recorder = voice_trace_recorder
                voice_trace_recorder_state_set = True
            app.state.conversation_history_repository = conversation_history_repository
            repository_state_set = True
            app.state.conversation_lifecycle_service = conversation_lifecycle_service
            lifecycle_service_state_set = True
            semantic_classifier_client = OllamaClassifierClient(
                model_id=model_settings.ollama_classifier_model
            )
            semantic_privacy_classifier = OllamaSemanticPrivacyClassifier(
                client=semantic_classifier_client,
                privacy_policy=policy.privacy,
                model_id=model_settings.ollama_classifier_model,
                model_digest_resolver=lambda timeout_seconds: (
                    semantic_classifier_client.resolve_model_digest(
                        timeout_seconds=timeout_seconds
                    )
                ),
            )
            app.state.semantic_privacy_classifier = semantic_privacy_classifier
            semantic_classifier_state_set = True
            app.state.persona_memory_provider = PersonaMemoryProvider(
                approved_repository=approved_memory_repository,
                scanner=privacy_scanner,
                classifier=semantic_privacy_classifier,
                admission_evaluator=create_rag_admission_evaluator(policy.privacy),
                index_sync=memory_index_sync,
                clock=clock,
            )
            persona_memory_provider_state_set = True
            app.state.addon_record_provider = AddonRecordProvider(
                temporary_record_repository
            )
            addon_record_provider_state_set = True
            app.state.rag_admission_service = RagAdmissionService(
                conversation_repository=conversation_history_repository,
                approved_repository=approved_memory_repository,
                privacy_scanner=privacy_scanner,
                semantic_classifier=semantic_privacy_classifier,
                evaluator=create_rag_admission_evaluator(policy.privacy),
                occurred_timezone=occurred_timezone,
                extractor_version=EXTRACTOR_VERSION,
            )
            rag_admission_service_state_set = True
            memory_extractor_client = OllamaMemoryExtractorClient(
                model_id=model_settings.ollama_extractor_model
            )
            consolidation_ollama_base_url = require_local_ollama_base_url(
                resolve_ollama_base_url()
            )
            memory_consolidation_client = OllamaMemoryExtractorClient(
                model_id=model_settings.ollama_extractor_model,
                base_url=consolidation_ollama_base_url,
            )
            memory_consolidation_classifier_client = OllamaClassifierClient(
                model_id=model_settings.ollama_classifier_model,
                base_url=consolidation_ollama_base_url,
            )
            memory_consolidation_privacy_classifier = (
                OllamaSemanticPrivacyClassifier(
                    client=memory_consolidation_classifier_client,
                    privacy_policy=policy.privacy,
                    model_id=model_settings.ollama_classifier_model,
                    model_digest_resolver=lambda timeout_seconds: (
                        memory_consolidation_classifier_client.resolve_model_digest(
                            timeout_seconds=timeout_seconds
                        )
                    ),
                )
            )
            memory_candidate_extractor = MemoryCandidateExtractor(
                client=memory_extractor_client,
                settings=formation_settings,
            )
            memory_formation_scheduler = MemoryFormationScheduler(
                worker=MemoryFormationWorker(
                    conversation_repository=conversation_history_repository,
                    extractor=memory_candidate_extractor,
                    admission_service=app.state.rag_admission_service,
                    domain_router=None,
                ),
                max_queue_age_seconds=formation_settings.max_queue_age_seconds,
                queue_maxsize=formation_settings.queue_maxsize,
            )
            await memory_formation_scheduler.start()
            memory_formation_scheduler_started = True
            consolidation_priority = ConsolidationPriorityProbe(
                conversation_repository=conversation_history_repository,
                formation_scheduler=memory_formation_scheduler,
                outbox_repository=outbox_repository,
            )
            memory_consolidation_scheduler = MemoryConsolidationScheduler(
                service=MemoryConsolidationService(
                    repository=approved_memory_repository,
                    planner=ConsolidationPlanner(
                        client=memory_consolidation_client,
                        max_output_tokens=consolidation_settings.max_output_tokens,
                        model_id=model_settings.ollama_extractor_model,
                        prompt_version=CONSOLIDATION_PROMPT_VERSION,
                        policy_version=policy.policy_version,
                    ),
                    privacy_reviewer=ConsolidationPrivacyReviewer(
                        scanner=privacy_scanner,
                        classifier=memory_consolidation_privacy_classifier,
                        evaluator=create_rag_admission_evaluator(policy.privacy),
                    ),
                    batch_size=consolidation_settings.batch_size,
                    llm_timeout_seconds=consolidation_settings.llm_timeout_seconds,
                    clock=clock,
                    model_id=model_settings.ollama_extractor_model,
                    prompt_version=CONSOLIDATION_PROMPT_VERSION,
                    policy_version=policy.policy_version,
                    reprocess_interval_seconds=consolidation_settings.interval_seconds,
                ),
                interval_seconds=consolidation_settings.interval_seconds,
                max_runtime_seconds=consolidation_settings.max_runtime_seconds,
                priority_probe=lambda: is_consolidation_eligible(
                    now=clock().astimezone(ZoneInfo(occurred_timezone)),
                    priority=consolidation_priority.read(),
                    idle_seconds=consolidation_settings.idle_seconds,
                    nightly_start_hour=0,
                    nightly_end_hour=6,
                ),
            )
            conversation_history_service = ConversationHistoryService(
                conversation_history_repository,
                history_sanitizer,
            )
            app_chat_service = _chat_runtime.create_chat_service(
                _chat_runtime.resolve_chat_runtime_config(
                    policy, model_settings, runtime_paths, occurred_timezone
                ),
                conversation_history_service,
                _chat_runtime.ChatRuntimeDependencies(
                    character_prompt_loader=_load_character_prompt,
                    prompt_builder=build_chat_prompt,
                    llm_response_generator=generate_llm_response,
                    input_token_counter=count_llm_input_tokens,
                    privacy_scanner=privacy_scanner,
                    semantic_classifier=semantic_privacy_classifier,
                    approved_memory_repository=approved_memory_repository,
                    memory_formation_submitter=memory_formation_scheduler,
                    clock=clock,
                ),
            )
            app.state.chat_service = app_chat_service
            chat_service_state_set = True
            audio_runtime_config = resolve_audio_runtime_config(
                model_settings, runtime_paths
            )
            app.state.audio_pipeline_service = create_audio_pipeline_service(
                audio_runtime_config,
            )
            audio_pipeline_state_set = True
            core_session_factory = None
            if resolve_livekit_settings() is not None:
                from app.stt.remote_whisper_client import RemoteWhisperTranscriber
                from app.tts.voicevox_client import create_voicevox_client

                core_transcriber = RemoteWhisperTranscriber(
                    audio_runtime_config.whisper_base_url
                )
                core_synthesizer = create_voicevox_client(
                    audio_runtime_config.voicevox_base_url
                )
                async def generate_core_reply_stream(
                    character: str,
                    history_session: object,
                    transcript: str,
                ) -> AsyncIterator[str]:
                    prompt, max_output_tokens = (
                        app_chat_service.prepare_unrecorded_generation(
                            character,
                            history_session,  # type: ignore[arg-type]
                            transcript,
                        )
                    )
                    async for text in llm_router.stream_response(
                        prompt,
                        max_output_tokens=max_output_tokens,
                        settings=model_settings,
                    ):
                        yield text
                    app_chat_service.record_successful_prompt_references(prompt)

                def submit_completed_core_turn(persisted_turn: object) -> None:
                    if not isinstance(persisted_turn, ConversationTurn):
                        raise TypeError("completed Core turn must be a ConversationTurn")
                    if persisted_turn.status is not TurnStatus.COMPLETED:
                        return
                    memory_formation_scheduler.submit(
                        MemoryFormationJob(
                            character_id=persisted_turn.character_id,
                            conversation_id=persisted_turn.conversation_id,
                            turn_id=persisted_turn.turn_id,
                        )
                    )

                core_session_factory = ProductionConversationCoreSessionFactory(
                    transcriber=core_transcriber,
                    synthesizer=core_synthesizer,
                    history_service=conversation_history_service,
                    completed_turn_observer=submit_completed_core_turn,
                    generate_reply_stream=generate_core_reply_stream,
                )
            livekit_api = await configure_production_resources(
                app,
                core_session_factory=core_session_factory,
            )
            chat_service_resolver = lambda: _app_chat_service(app)
            _chat_runtime.register_default_chat_service_resolver(chat_service_resolver)
            resolver_registered = True
            memory_index_scheduler.start()
            memory_index_scheduler_started = True
            await memory_consolidation_scheduler.start()
            memory_consolidation_scheduler_started = True
            yield
        finally:
            cleanup_errors: list[BaseException] = []

            async def run_cleanup(operation: Awaitable[None]) -> None:
                try:
                    await operation
                except BaseException as error:
                    cleanup_errors.append(error)

            if livekit_api is not None:
                await run_cleanup(app.state.livekit_runtime_manager.stop_all())
                await run_cleanup(livekit_api.aclose())
            if memory_consolidation_scheduler_started:
                await run_cleanup(memory_consolidation_scheduler.stop())
            if memory_formation_scheduler_started:
                await run_cleanup(memory_formation_scheduler.stop())
            if memory_index_scheduler_started:
                await run_cleanup(memory_index_scheduler.stop())
            with ExitStack() as cleanup:
                if livekit_api is not None:
                    for state_name in (
                        "livekit_room_manager",
                        "livekit_session_repository",
                        "livekit_runtime_manager",
                        "livekit_token_signer",
                        "livekit_bootstrap_service",
                        "livekit_core_events",
                        "livekit_url",
                    ):
                        cleanup.callback(delattr, app.state, state_name)
                if voice_trace_recorder_state_set:
                    cleanup.callback(delattr, app.state, "voice_trace_recorder")
                if voice_measurement_kind_state_set:
                    cleanup.callback(delattr, app.state, "voice_measurement_kind")
                if chat_service_state_set:
                    cleanup.callback(delattr, app.state, "chat_service")
                if audio_pipeline_state_set:
                    cleanup.callback(delattr, app.state, "audio_pipeline_service")
                    cleanup.callback(app.state.audio_pipeline_service.close)
                if core_synthesizer is not None:
                    cleanup.callback(core_synthesizer.close)
                if core_transcriber is not None:
                    cleanup.callback(core_transcriber.close)
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
                if lifecycle_service_state_set:
                    cleanup.callback(
                        delattr,
                        app.state,
                        "conversation_lifecycle_service",
                    )
                if semantic_classifier_state_set:
                    cleanup.callback(
                        delattr,
                        app.state,
                        "semantic_privacy_classifier",
                    )
                if persona_memory_provider_state_set:
                    cleanup.callback(delattr, app.state, "persona_memory_provider")
                if addon_record_provider_state_set:
                    cleanup.callback(delattr, app.state, "addon_record_provider")
                if rag_admission_service_state_set:
                    cleanup.callback(
                        delattr,
                        app.state,
                        "rag_admission_service",
                    )
                if semantic_classifier_client is not None:
                    cleanup.callback(semantic_classifier_client.close)
                if memory_extractor_client is not None:
                    cleanup.callback(memory_extractor_client.close)
                if memory_consolidation_client is not None:
                    cleanup.callback(memory_consolidation_client.close)
                if memory_consolidation_classifier_client is not None:
                    cleanup.callback(memory_consolidation_classifier_client.close)
            if cleanup_errors:
                raise cleanup_errors[0]


app = FastAPI(lifespan=lifespan)

app.include_router(chat_router)
app.include_router(conversations_router)
app.include_router(memory_management_router)
app.include_router(livekit_router)
app.include_router(ws_router)


@app.get("/")
def health_check() -> dict[str, str]:
    return {"status": "ok"}

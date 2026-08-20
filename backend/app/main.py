from contextlib import ExitStack, asynccontextmanager
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
import sqlite3
from pathlib import Path
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
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.index_outbox_repository import IndexOutboxRepository
from app.model_settings import resolve_model_settings
from app.prompting import BuiltPrompt, CharacterPrompt, PromptMessage
from app.privacy.history_sanitizer import create_history_sanitizer
from app.privacy.scanner import create_privacy_scanner
from app.privacy.semantic.classifier import OllamaSemanticPrivacyClassifier
from app.privacy.semantic.ollama_classifier_client import OllamaClassifierClient
from app.routers.chat import router as chat_router
from app.routers.conversations import router as conversations_router
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

load_dotenv()

MEMORY_EFFECTIVE_TIMEZONE_ENV = "MEMORY_EFFECTIVE_TIMEZONE"
DEFAULT_MEMORY_EFFECTIVE_TIMEZONE = "Asia/Tokyo"
DOGFOOD_BACKUP_DIR_ENV = "DOGFOOD_BACKUP_DIR"
DOGFOOD_BACKUP_RETENTION_COUNT_ENV = "DOGFOOD_BACKUP_RETENTION_COUNT"


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
    model_settings = resolve_model_settings(os.environ)
    effective_timezone = iana_timezone_environment_value(
        MEMORY_EFFECTIVE_TIMEZONE_ENV,
        DEFAULT_MEMORY_EFFECTIVE_TIMEZONE,
    )
    repository_root = Path(__file__).resolve().parents[2]
    runtime_paths = resolve_runtime_paths(os.environ, repository_root)
    initialize_runtime_data_root(runtime_paths, repository_root)
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
        clock = lambda: datetime.now(UTC)
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
        memory_index_sync = MemoryIndexSync(
            approved_repository=approved_memory_repository,
            outbox_repository=IndexOutboxRepository(
                database_path=runtime_paths.persona_memory_sqlite_path,
                clock=clock,
            ),
            chroma_path=runtime_paths.chroma_path,
            runtime_report_dir=runtime_paths.runtime_report_dir,
            embedder=embed_text,
            clock=clock,
        )
        memory_index_scheduler = MemoryIndexScheduler(memory_index_sync)
        chat_service_resolver = None
        repository_state_set = False
        lifecycle_service_state_set = False
        resolver_registered = False
        chat_service_state_set = False
        audio_pipeline_state_set = False
        semantic_classifier_state_set = False
        rag_admission_service_state_set = False
        semantic_classifier_client = None
        memory_index_scheduler_started = False
        try:
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
            app.state.rag_admission_service = RagAdmissionService(
                conversation_repository=conversation_history_repository,
                approved_repository=approved_memory_repository,
                privacy_scanner=privacy_scanner,
                semantic_classifier=semantic_privacy_classifier,
                evaluator=create_rag_admission_evaluator(policy.privacy),
                effective_timezone=effective_timezone,
            )
            rag_admission_service_state_set = True
            app_chat_service = _chat_runtime.create_chat_service(
                _chat_runtime.resolve_chat_runtime_config(
                    policy, model_settings, runtime_paths
                ),
                ConversationHistoryService(
                    conversation_history_repository,
                    history_sanitizer,
                ),
                _chat_runtime.ChatRuntimeDependencies(
                    character_prompt_loader=_load_character_prompt,
                    prompt_builder=build_chat_prompt,
                    llm_response_generator=generate_llm_response,
                    input_token_counter=count_llm_input_tokens,
                ),
            )
            app.state.chat_service = app_chat_service
            chat_service_state_set = True
            app.state.audio_pipeline_service = create_audio_pipeline_service(
                resolve_audio_runtime_config(model_settings, runtime_paths),
            )
            audio_pipeline_state_set = True
            chat_service_resolver = lambda: _app_chat_service(app)
            _chat_runtime.register_default_chat_service_resolver(chat_service_resolver)
            resolver_registered = True
            memory_index_scheduler.start()
            memory_index_scheduler_started = True
            yield
        finally:
            if memory_index_scheduler_started:
                await memory_index_scheduler.stop()
            with ExitStack() as cleanup:
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
                if rag_admission_service_state_set:
                    cleanup.callback(
                        delattr,
                        app.state,
                        "rag_admission_service",
                    )
                if semantic_classifier_client is not None:
                    cleanup.callback(semantic_classifier_client.close)


app = FastAPI(lifespan=lifespan)

app.include_router(chat_router)
app.include_router(conversations_router)
app.include_router(ws_router)


@app.get("/")
def health_check() -> dict[str, str]:
    return {"status": "ok"}

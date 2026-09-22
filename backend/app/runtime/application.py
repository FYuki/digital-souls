"""lifespan全体の資源所有と起動・停止順を管理するapplication coordinator。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from app import _chat_runtime
from app.character_life.runtime import Settings as LifeSettings
from app.environment import iana_timezone_environment_value
from app.inference import (
    InferenceTarget,
    default_provider_registry,
    resolve_inference_settings,
)
from app.inference.config import (
    InferenceSettings,
    reject_legacy_inference_environment,
)
from app.llm import router as llm_router
from app.model_settings import ModelSettings, resolve_model_settings
from app.prompting import BuiltPrompt, PromptMessage
from app.runtime.audio import AudioResources, VOICE_MEASUREMENT_KIND_ENV
from app.runtime.chat import ChatResources
from app.runtime.history import HistoryResources
from app.runtime.inference import InferenceResources
from app.runtime.life import LifeResources
from app.runtime.memory import MemoryResources
from app.runtime.privacy import PrivacyResources
from app.runtime.screen import ScreenResources
from app.runtime.tools import ToolResources
from app.runtime_data_root import (
    initialize_runtime_data_root,
    remove_legacy_chroma_index_once,
)
from app.runtime_paths import (
    RuntimePaths,
    resolve_runtime_paths,
    runtime_paths_projection,
)
from app.conversation_history.sqlite_lease import acquire_maintenance_lease
from app.memory.memory_policy import resolved_memory_policy
from app.tool_use.runtime import ToolSettings
from app.voice_measurement_memory import formation_disabled

if TYPE_CHECKING:
    from fastapi import FastAPI
    from app.livekit_transport.production import (
        ProductionConversationCoreSessionFactory,
    )

MEMORY_OCCURRED_TIMEZONE_ENV = "MEMORY_OCCURRED_TIMEZONE"
DEFAULT_MEMORY_OCCURRED_TIMEZONE = "Asia/Tokyo"


def log_runtime_configuration(paths: RuntimePaths) -> None:
    logging.getLogger(__name__).info(
        "Runtime configuration: %s", runtime_paths_projection(paths)
    )


@dataclass(frozen=True)
class LifespanWiring:
    """main.pyが供給する業務callback。資源の所有は持たない。"""

    create_chat_service: Callable[
        [ApplicationRuntime], _chat_runtime.ChatService
    ]
    create_core_session_factory: Callable[
        [ApplicationRuntime], ProductionConversationCoreSessionFactory
    ]
    validate_screen_context: Callable[[ApplicationRuntime, str, UUID], None]
    episodic_entity_labels: Callable[[str], Mapping[str, str]]


class ApplicationRuntime:
    """lifespanで確保した資源の所有と、起動・停止順を管理する。"""

    def __init__(self, app: FastAPI, environ: Mapping[str, str]) -> None:
        self.app = app
        self.environ = environ
        self.inference_settings: InferenceSettings | None = None
        self.tool_settings: ToolSettings | None = None
        self.model_settings: ModelSettings | None = None
        self.occurred_timezone = DEFAULT_MEMORY_OCCURRED_TIMEZONE
        self.repository_root = Path(__file__).resolve().parents[3]
        self.runtime_paths: RuntimePaths | None = None
        self.disable_memory_formation = False
        self.clock: Callable[[], datetime] | None = None
        self.inference: InferenceResources | None = None
        self.screen: ScreenResources | None = None
        self.audio: AudioResources | None = None
        self.privacy: PrivacyResources | None = None
        self.history: HistoryResources | None = None
        self.memory = MemoryResources()
        self.tools = ToolResources()
        self.life = LifeResources()
        self.chat = ChatResources()

    def generate_llm_response(
        self, prompt: BuiltPrompt, *, max_output_tokens: int
    ) -> str:
        assert self.model_settings is not None
        return llm_router.generate_response(
            prompt,
            max_output_tokens=max_output_tokens,
            settings=self.model_settings,
        )

    def count_llm_input_tokens(
        self, messages: tuple[PromptMessage, ...]
    ) -> int:
        assert self.model_settings is not None
        return llm_router.count_input_tokens(
            messages, settings=self.model_settings
        )

    def prepare(self) -> None:
        """保護区間より前の設定解決と基底資源の確保を行う。"""
        environ = self.environ
        reject_legacy_inference_environment(environ)
        self.inference_settings = resolve_inference_settings(
            environ,
            default_provider_registry(),
        )
        self.tool_settings = ToolSettings.load(environ.get("DS_MCP_CONFIG"))
        chat_target = self.inference_settings.target(InferenceTarget.CHAT)
        chat_output_tokens = chat_target.max_output_tokens
        if chat_output_tokens is None:
            raise AssertionError("chat target requires an output limit")
        self.model_settings = resolve_model_settings(
            environ,
            chat_context_tokens=chat_target.max_input_tokens
            + chat_output_tokens,
            assistant_max_generation_tokens=chat_output_tokens,
        )
        self.occurred_timezone = iana_timezone_environment_value(
            MEMORY_OCCURRED_TIMEZONE_ENV,
            DEFAULT_MEMORY_OCCURRED_TIMEZONE,
        )
        self.runtime_paths = resolve_runtime_paths(
            environ, self.repository_root
        )
        self.disable_memory_formation = formation_disabled(
            environ,
            environment_id=self.runtime_paths.environment_id,
            measurement_kind=environ.get(
                VOICE_MEASUREMENT_KIND_ENV, "automated_test"
            ),
        )
        if (
            self.disable_memory_formation
            and LifeSettings.load(dict(environ)).enabled
        ):
            raise ValueError(
                "controlled memory isolation requires Character Life disabled"
            )
        self.inference = InferenceResources.create(environ)
        self.screen = ScreenResources.create(environ)
        self.inference.probe()
        initialize_runtime_data_root(self.runtime_paths, self.repository_root)
        self.audio = AudioResources.create_measurement(
            environ, self.runtime_paths, self.repository_root
        )
        from app.restore_intent import require_no_restore_intent

        require_no_restore_intent(self.runtime_paths.restore_intent_path)
        self.audio.remove_controlled_policy_record(self.runtime_paths)
        self.privacy = PrivacyResources(resolved_memory_policy())
        remove_legacy_chroma_index_once(
            self.runtime_paths, self.repository_root
        )
        log_runtime_configuration(self.runtime_paths)
        self.privacy.build_scanner_sanitizer()
        self.history = HistoryResources.resolve(self.runtime_paths)

    @contextmanager
    def acquire_persistent_state(self) -> Iterator[None]:
        """maintenance lease保持下で履歴・記憶の永続化資源を構築する。"""
        history = self.history
        inference = self.inference
        paths = self.runtime_paths
        assert (
            history is not None and inference is not None and paths is not None
        )
        with acquire_maintenance_lease(history.config.database_path) as lease:
            history.lease = lease
            history.initialize_schema(paths, self.repository_root)
            self.memory.initialize_schema(paths, self.repository_root)

            def clock() -> datetime:
                return datetime.now(UTC)

            self.clock = clock
            history.build(clock)
            self.memory.build(
                runtime_paths=paths,
                history=history,
                inference=inference.runtime,
                environ=self.environ,
                clock=clock,
            )
            yield

    async def start(self, wiring: LifespanWiring) -> None:
        """保護区間の起動順序。各ownerの公開・起動を現行順で実行する。"""
        app = self.app
        inference = self.inference
        history = self.history
        privacy = self.privacy
        audio = self.audio
        screen = self.screen
        memory = self.memory
        paths = self.runtime_paths
        model_settings = self.model_settings
        assert (
            inference is not None
            and history is not None
            and privacy is not None
            and audio is not None
            and screen is not None
            and paths is not None
            and model_settings is not None
        )
        inference.publish(app)
        audio.publish_measurement(app)
        history.publish_repository(app)
        screen.publish(
            app,
            validate_context=lambda character_id, conversation_id: (
                wiring.validate_screen_context(
                    self, character_id, conversation_id
                )
            ),
            inference=inference,
        )
        history.publish_services(app)
        privacy.build_classifier(inference)
        privacy.publish(app)
        memory.build_management(
            app,
            privacy=privacy,
            history=history,
            occurred_timezone=self.occurred_timezone,
        )
        memory.build_clients(inference, privacy)
        memory.build_formation(
            disable=self.disable_memory_formation,
            history=history,
            privacy=privacy,
            inference=inference,
            occurred_timezone=self.occurred_timezone,
            entity_labels=wiring.episodic_entity_labels,
        )
        await memory.start_formation()
        memory.build_consolidation(
            inference=inference,
            history=history,
            privacy=privacy,
            occurred_timezone=self.occurred_timezone,
        )
        assert privacy.history_sanitizer is not None
        history.build_service(privacy.history_sanitizer)
        self.life.load_settings(self.environ)
        self.life.check_enabled(inference)
        assert self.tool_settings is not None
        scanner = privacy.scanner
        consolidation_classifier = memory.consolidation_privacy_classifier
        assert scanner is not None and consolidation_classifier is not None
        self.tools.build(
            self.tool_settings,
            inference=inference,
            scanner=scanner,
            settings_path=paths.data_root / "addon-settings.json",
            classifier=consolidation_classifier,
        )
        self.tools.publish_addons(app)
        await self.tools.start()
        self.tools.publish_service(app, inference)
        await self.life.start_if_enabled(
            app=app,
            paths=paths,
            repository_root=self.repository_root,
            inference=inference,
            tools=self.tools,
            history=history,
            privacy_classifier=consolidation_classifier,
            model_settings=model_settings,
            input_token_counter=self.count_llm_input_tokens,
        )
        self.chat.publish(app, wiring.create_chat_service(self))
        audio.build_pipeline(app, model_settings, paths)
        core_session_factory = None
        if audio.livekit_enabled():
            audio.build_core_clients()
            core_session_factory = wiring.create_core_session_factory(self)
        history_repository = history.repository
        screen_service = screen.service
        assert history_repository is not None and screen_service is not None
        await audio.configure(
            app,
            core_session_factory,
            screen_session_revoker=screen_service,
            conversations=history_repository,
        )
        self.chat.register_resolver(app)
        memory.start_index()
        await memory.start_consolidation(self.disable_memory_formation)
        audio.record_controlled_policy(
            paths, disabled=self.disable_memory_formation
        )

    def abort(self) -> None:
        """prepare/acquire_persistent_stateの失敗で残った資源を回収する。

        shutdown()はstart()進入後の回収経路。それより前の失敗では
        構築済みのinference runtimeだけをここで解放する。
        """
        if self.inference is not None:
            self.inference.close()

    async def shutdown(self) -> None:
        """確保済み資源を現行の停止順で回収する。"""
        app = self.app
        audio = self.audio
        screen = self.screen
        history = self.history
        privacy = self.privacy
        inference = self.inference
        memory = self.memory
        assert (
            audio is not None
            and screen is not None
            and history is not None
            and privacy is not None
            and inference is not None
        )
        cleanup_errors: list[BaseException] = []

        async def run_cleanup(operation: Awaitable[None]) -> None:
            try:
                await operation
            except BaseException as error:
                cleanup_errors.append(error)

        livekit = audio.livekit_resources
        if livekit is not None:
            await run_cleanup(
                livekit.bootstrap_service.cancel_all_preparations()
            )
            await run_cleanup(livekit.runtime_manager.stop_all())
            await run_cleanup(livekit.api.aclose())
        if self.life.started and self.life.runtime is not None:
            await run_cleanup(self.life.runtime.close())
        if hasattr(app.state, "character_life_runtime"):
            del app.state.character_life_runtime
        if self.tools.runtime is not None:
            await run_cleanup(self.tools.runtime.close())
        if hasattr(app.state, "tool_service"):
            del app.state.tool_service
        if hasattr(app.state, "action_policy"):
            del app.state.action_policy
        if memory.consolidation_started:
            assert memory.consolidation_scheduler is not None
            await run_cleanup(memory.consolidation_scheduler.stop())
        if memory.formation_started:
            assert memory.formation_scheduler is not None
            await run_cleanup(memory.formation_scheduler.stop())
        if memory.index_started:
            assert memory.index_scheduler is not None
            await run_cleanup(memory.index_scheduler.stop())
        with ExitStack() as cleanup:
            if livekit is not None:
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
            if audio.recorder_published:
                cleanup.callback(delattr, app.state, "voice_trace_recorder")
            if audio.kind_published:
                cleanup.callback(delattr, app.state, "voice_measurement_kind")
            if self.chat.published:
                cleanup.callback(delattr, app.state, "chat_service")
            if audio.pipeline_published:
                cleanup.callback(delattr, app.state, "audio_pipeline_service")
                cleanup.callback(app.state.audio_pipeline_service.close)
            if audio.core_synthesizer is not None:
                cleanup.callback(audio.core_synthesizer.close)
            if audio.core_transcriber is not None:
                cleanup.callback(audio.core_transcriber.close)
            if self.chat.resolver_registered and self.chat.resolver is not None:
                cleanup.callback(
                    _chat_runtime.clear_default_chat_service_resolver,
                    self.chat.resolver,
                )
            if history.repository_published:
                cleanup.callback(
                    delattr,
                    app.state,
                    "conversation_history_repository",
                )
            if history.lifecycle_published:
                cleanup.callback(
                    delattr,
                    app.state,
                    "conversation_lifecycle_service",
                )
            if history.ui_settings_published:
                cleanup.callback(
                    delattr,
                    app.state,
                    "ui_settings_repository",
                )
            if privacy.classifier_published:
                cleanup.callback(
                    delattr,
                    app.state,
                    "semantic_privacy_classifier",
                )
            if inference.state_published:
                cleanup.callback(delattr, app.state, "inference_router")
            if inference.router_registered:
                cleanup.callback(
                    llm_router.clear_inference_router,
                    inference.runtime.router,
                )
            if memory.semantic_published:
                cleanup.callback(delattr, app.state, "semantic_store")
                cleanup.callback(
                    delattr, app.state, "semantic_memory_management"
                )
            if memory.episodic_published:
                cleanup.callback(
                    delattr, app.state, "episodic_memory_management"
                )
            if memory.persona_published:
                cleanup.callback(
                    delattr, app.state, "persona_memory_provider"
                )
            if memory.addon_published:
                cleanup.callback(
                    delattr, app.state, "addon_record_provider"
                )
            if memory.rag_published:
                cleanup.callback(
                    delattr, app.state, "rag_admission_service"
                )
            if screen.published:
                cleanup.callback(
                    delattr, app.state, "screen_perception_service"
                )
                cleanup.callback(delattr, app.state, "screen_http_security")
            if privacy.classifier_client is not None:
                cleanup.callback(privacy.classifier_client.close)
            if memory.extractor_client is not None:
                cleanup.callback(memory.extractor_client.close)
            if memory.consolidation_client is not None:
                cleanup.callback(memory.consolidation_client.close)
            if memory.consolidation_classifier_client is not None:
                cleanup.callback(memory.consolidation_classifier_client.close)
            cleanup.callback(inference.close)
            cleanup.callback(delattr, app.state, "inference_health")
        if cleanup_errors:
            raise cleanup_errors[0]

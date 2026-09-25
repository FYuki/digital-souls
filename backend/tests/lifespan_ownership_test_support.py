"""lifespanの資源所有・起動停止順・失敗時回収の契約を固定する。

#482でmain.pyのlifespanを資源所有単位へ分割する前に、現在の観測可能な
振る舞いを固定する。起動順・停止順・起動途中失敗時の回収・cleanup失敗時の
継続・process global登録の解除・optional無効・同一app再起動の各契約は、
分割後の実装でも維持されなければならない。
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conversation_history_test_support import CONVERSATION_ID


def _injected(label: str) -> ValueError:
    return ValueError(f"injected {label}")


def _assert_event_subsequence(events: list[str], expected: list[str]) -> None:
    observed = [event for event in events if event in expected]
    assert observed == expected


def _record_inference_close(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    from app.inference.runtime import InferenceRuntime

    original_close = InferenceRuntime.close

    def close(self: InferenceRuntime) -> None:
        events.append("inference:close")
        original_close(self)

    monkeypatch.setattr(InferenceRuntime, "close", close)


def _record_lease_close(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    from app.conversation_history.sqlite_lease import SQLiteLease

    original_close = SQLiteLease.close

    def close(self: SQLiteLease) -> None:
        events.append(f"lease-close:{id(self)}")
        original_close(self)

    monkeypatch.setattr(SQLiteLease, "close", close)


def _capture_maintenance_lease(
    monkeypatch: pytest.MonkeyPatch, captured: list[Any]
) -> None:
    import app.runtime.application as application_runtime

    real_acquire = application_runtime.acquire_maintenance_lease

    @contextmanager
    def acquire(database_path: Any) -> Any:
        with real_acquire(database_path) as lease:
            captured.append(lease)
            yield lease

    monkeypatch.setattr(
        application_runtime, "acquire_maintenance_lease", acquire
    )


def _spy_inference_router_registration(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    from app import main

    register = main.llm_router.register_inference_router
    clear = main.llm_router.clear_inference_router

    def record_register(router: Any) -> None:
        events.append("router:register")
        register(router)

    def record_clear(router: Any) -> None:
        events.append("router:clear")
        clear(router)

    monkeypatch.setattr(
        main.llm_router, "register_inference_router", record_register
    )
    monkeypatch.setattr(
        main.llm_router, "clear_inference_router", record_clear
    )


def _spy_chat_resolver_registration(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    from app import main

    register = main._chat_runtime.register_default_chat_service_resolver
    clear = main._chat_runtime.clear_default_chat_service_resolver

    def record_register(resolver: Any) -> None:
        events.append("resolver:register")
        register(resolver)

    def record_clear(resolver: Any) -> None:
        events.append("resolver:clear")
        clear(resolver)

    monkeypatch.setattr(
        main._chat_runtime,
        "register_default_chat_service_resolver",
        record_register,
    )
    monkeypatch.setattr(
        main._chat_runtime,
        "clear_default_chat_service_resolver",
        record_clear,
    )


def _spy_chat_service_create(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> None:
    from app import main

    create = main._chat_runtime.create_chat_service

    def record_create(*args: Any, **kwargs: Any) -> Any:
        events.append("chat:create")
        return create(*args, **kwargs)

    monkeypatch.setattr(
        main._chat_runtime, "create_chat_service", record_create
    )


def _stub_tool_runtime(
    events: list[str], *, fail_start: bool = False, fail_close: bool = False
) -> Any:
    class StubToolRuntime:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self._events = events
            self.management = object()
            self.events = None
            self.notifications = object()
            self.action_policy = object()
            self.service = SimpleNamespace(
                sanitizer=object(),
                bindings=object(),
                interrupted=lambda *_args, **_kwargs: None,
            )
            self.gate = SimpleNamespace(recovery=None)

        async def start(self) -> None:
            self._events.append("tool:start")
            if fail_start:
                raise _injected("tool start")

        async def close(self) -> None:
            self._events.append("tool:close")
            if fail_close:
                raise _injected("tool close")

    return StubToolRuntime


def _async_scheduler(
    name: str,
    events: list[str],
    *,
    fail_start: bool = False,
    self_stop: bool = False,
    fail_stop: bool = False,
) -> Any:
    instances: list[Any] = []

    class StubScheduler:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.start_calls = 0
            self.stop_calls = 0
            instances.append(self)

        async def start(self) -> None:
            self.start_calls += 1
            events.append(f"{name}:start")
            if fail_start:
                if self_stop:
                    await self.stop()
                raise _injected(f"{name} start")

        async def stop(self) -> None:
            self.stop_calls += 1
            events.append(f"{name}:stop")
            if fail_stop:
                raise _injected(f"{name} stop")

        def submit(self, _job: Any) -> None:
            pass

        def is_busy(self) -> bool:
            return False

    StubScheduler.instances = instances
    return StubScheduler


def _index_scheduler(events: list[str], *, fail_start: bool = False) -> Any:
    class StubIndexScheduler:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.stop_calls = 0

        def start(self) -> None:
            events.append("index:start")
            if fail_start:
                raise _injected("index start")

        async def stop(self) -> None:
            self.stop_calls += 1
            events.append("index:stop")

    return StubIndexScheduler


def _audio_service(events: list[str], *, fail_close: bool = False) -> Any:
    class StubAudioPipelineService:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            events.append("audio:close")
            if fail_close:
                raise _injected("audio close")

    return StubAudioPipelineService()


def _life_runtime(events: list[str], *, fail_start: bool = False) -> Any:
    class StubLifeRuntime:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self._events = events

        async def start(self) -> None:
            self._events.append("life:start")
            if fail_start:
                # LifeRuntime.startは起動失敗時に内部で自己回収してから例外を送出する。
                await self.close()
                raise _injected("life start")

        async def close(self) -> None:
            self._events.append("life:close")

    return StubLifeRuntime


def _install_owner_stubs(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    tool: Any = None,
    formation: Any = None,
    index: Any = None,
    consolidation: Any = None,
    audio: Any = None,
) -> None:
    import app.runtime.audio as audio_runtime
    import app.runtime.memory as memory_runtime
    import app.runtime.tools as tool_runtime_module

    monkeypatch.setattr(
        tool_runtime_module,
        "ToolRuntime",
        tool if tool is not None else _stub_tool_runtime(events),
    )
    monkeypatch.setattr(
        memory_runtime,
        "CombinedFormationScheduler",
        formation
        if formation is not None
        else _async_scheduler("formation", events),
    )
    monkeypatch.setattr(
        memory_runtime,
        "MemoryIndexScheduler",
        index if index is not None else _index_scheduler(events),
    )
    monkeypatch.setattr(
        memory_runtime,
        "MemoryConsolidationScheduler",
        consolidation
        if consolidation is not None
        else _async_scheduler("consolidation", events),
    )
    audio_service = audio if audio is not None else _audio_service(events)

    def create_audio_pipeline_service(_config: Any) -> Any:
        events.append("audio:create")
        return audio_service

    monkeypatch.setattr(
        audio_runtime,
        "create_audio_pipeline_service",
        create_audio_pipeline_service,
    )


def _install_livekit_stubs(
    monkeypatch: pytest.MonkeyPatch, events: list[str], *, fail_configure: bool
) -> None:
    import app.livekit_transport.core_factory as core_factory
    import app.livekit_transport.production as production
    import app.stt.remote_whisper_client as whisper_client
    import app.tts.voicevox_client as voicevox_client

    class StubTranscriber:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def close(self) -> None:
            events.append("transcriber:close")

    class StubSynthesizer:
        def close(self) -> None:
            events.append("synthesizer:close")

    monkeypatch.setattr(
        whisper_client, "RemoteWhisperTranscriber", StubTranscriber
    )
    monkeypatch.setattr(
        voicevox_client,
        "create_voicevox_client",
        lambda *_args, **_kwargs: StubSynthesizer(),
    )
    monkeypatch.setattr(
        core_factory,
        "ProductionConversationCoreSessionFactory",
        lambda **_kwargs: object(),
    )

    class StubBootstrapService:
        async def cancel_all_preparations(self) -> None:
            events.append("livekit:cancel_preparations")

    class StubRuntimeManager:
        async def stop_all(self) -> None:
            events.append("livekit:stop_all")

    class StubLiveKitApi:
        async def aclose(self) -> None:
            events.append("livekit:aclose")

    async def configure(**_kwargs: Any) -> Any:
        if fail_configure:
            raise _injected("livekit configure")
        events.append("livekit:configure")
        return SimpleNamespace(
            api=StubLiveKitApi(),
            room_manager=object(),
            session_repository=object(),
            runtime_manager=StubRuntimeManager(),
            token_signer=object(),
            bootstrap_service=StubBootstrapService(),
            core_events=object(),
            url="ws://livekit.test",
        )

    monkeypatch.setattr(
        production, "configure_production_resources", configure
    )
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit.test")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret")


def _enable_character_life(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DS_CHARACTER_LIFE_ENABLED", "true")
    monkeypatch.setenv("DS_CHARACTER_LIFE_CRON", "0 0 1 1 *")
    monkeypatch.setenv(
        "INFERENCE_TARGET_CHARACTER_LIFE", "ollama/gemma4:e4b"
    )
    monkeypatch.setenv(
        "INFERENCE_TARGET_CHARACTER_LIFE_MAX_INPUT_TOKENS", "12000"
    )
    monkeypatch.setenv(
        "INFERENCE_TARGET_CHARACTER_LIFE_MAX_OUTPUT_TOKENS", "1024"
    )


_PRE_TRY_BOUNDARIES = [
    "initialize_runtime_data_root",
    "require_no_restore_intent",
    "resolved_memory_policy",
    "create_privacy_scanner",
    "create_history_sanitizer",
    "resolve_conversation_history_config",
    "acquire_maintenance_lease",
    "ensure_schema_backup_gate",
    "initialize_conversation_history_schema",
    "initialize_persona_memory_schema",
    "ConversationWalCleanup",
    "ConversationHistoryRepository",
    "ConversationLifecycleService",
    "UiSettingsRepository",
    "ApprovedMemoryRepository",
    "EpisodicRepository",
    "ConversationSourceGuard",
    "CombinedMemoryReadRepository",
    "EpisodicReadRepository",
    "WithSemanticReadRepository",
    "ResponseProvenanceRecorder",
    "IndexOutboxRepository",
    "MemoryInferenceEmbedder",
    "MemoryIndexSync",
    "TemporaryProviderRecordRepository",
    "MemoryIndexScheduler",
    "resolve_memory_formation_settings",
    "resolve_memory_consolidation_settings",
]


_PRE_TRY_MODULES: dict[str, str] = {
    "initialize_runtime_data_root": "app.runtime.application",
    "resolved_memory_policy": "app.runtime.application",
    "create_privacy_scanner": "app.runtime.privacy",
    "create_history_sanitizer": "app.runtime.privacy",
    "resolve_conversation_history_config": "app.runtime.history",
    "acquire_maintenance_lease": "app.runtime.application",
    "ensure_schema_backup_gate": "app.runtime.history",
    "initialize_conversation_history_schema": "app.runtime.history",
    "ConversationWalCleanup": "app.runtime.history",
    "ConversationHistoryRepository": "app.runtime.history",
    "ConversationLifecycleService": "app.runtime.history",
    "UiSettingsRepository": "app.runtime.history",
    "ApprovedMemoryRepository": "app.runtime.memory",
    "EpisodicRepository": "app.runtime.memory",
    "ConversationSourceGuard": "app.runtime.memory",
    "CombinedMemoryReadRepository": "app.runtime.memory",
    "EpisodicReadRepository": "app.runtime.memory",
    "WithSemanticReadRepository": "app.runtime.memory",
    "IndexOutboxRepository": "app.runtime.memory",
    "MemoryInferenceEmbedder": "app.runtime.memory",
    "MemoryIndexSync": "app.runtime.memory",
    "TemporaryProviderRecordRepository": "app.runtime.memory",
    "MemoryIndexScheduler": "app.runtime.memory",
    "resolve_memory_formation_settings": "app.runtime.memory",
    "resolve_memory_consolidation_settings": "app.runtime.memory",
}


def _inject_pre_try_failure(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    failure = Mock(side_effect=_injected(boundary))
    if boundary == "require_no_restore_intent":
        import app.restore_intent as restore_intent

        monkeypatch.setattr(
            restore_intent, "require_no_restore_intent", failure
        )
    elif boundary == "initialize_persona_memory_schema":
        from app.memory.persistence import schema as persona_schema

        monkeypatch.setattr(
            persona_schema, "initialize_persona_memory_schema", failure
        )
    elif boundary == "ResponseProvenanceRecorder":
        from app.memory import response_provenance_recorder

        monkeypatch.setattr(
            response_provenance_recorder, "ResponseProvenanceRecorder", failure
        )
    else:
        module = importlib.import_module(_PRE_TRY_MODULES[boundary])
        monkeypatch.setattr(module, boundary, failure)


_INSIDE_TRY_EXPECTED: dict[str, list[str]] = {
    "screen_perception": ["router:clear", "inference:close"],
    "semantic_classifier_client": ["router:clear", "inference:close"],
    "semantic_store": ["router:clear", "inference:close"],
    "persona_memory_provider": ["router:clear", "inference:close"],
    "episodic_memory_management": ["router:clear", "inference:close"],
    "addon_record_provider": ["router:clear", "inference:close"],
    "rag_admission_service": ["router:clear", "inference:close"],
    "memory_inference_client": ["router:clear", "inference:close"],
    "memory_candidate_extractor": ["router:clear", "inference:close"],
    "preference_scheduler_build": ["router:clear", "inference:close"],
    "episodic_scheduler_build": ["router:clear", "inference:close"],
    "formation_start": [
        "formation:stop",
        "router:clear",
        "inference:close",
    ],
    "consolidation_probe": [
        "formation:stop",
        "router:clear",
        "inference:close",
    ],
    "consolidation_planner": [
        "formation:stop",
        "router:clear",
        "inference:close",
    ],
    "conversation_history_service": [
        "formation:stop",
        "router:clear",
        "inference:close",
    ],
    "life_settings": [
        "formation:stop",
        "router:clear",
        "inference:close",
    ],
    "tool_construct": [
        "formation:stop",
        "router:clear",
        "inference:close",
    ],
    "tool_start": [
        "tool:close",
        "formation:stop",
        "router:clear",
        "inference:close",
    ],
    "chat_runtime_config": [
        "tool:close",
        "formation:stop",
        "router:clear",
        "inference:close",
    ],
    "chat_service": [
        "tool:close",
        "formation:stop",
        "router:clear",
        "inference:close",
    ],
    "audio_runtime_config": [
        "tool:close",
        "formation:stop",
        "router:clear",
        "inference:close",
    ],
    "audio_pipeline_service": [
        "tool:close",
        "formation:stop",
        "router:clear",
        "inference:close",
    ],
    "configure_production": [
        "tool:close",
        "formation:stop",
        "audio:close",
        "router:clear",
        "inference:close",
    ],
    "register_chat_resolver": [
        "tool:close",
        "formation:stop",
        "audio:close",
        "router:clear",
        "inference:close",
    ],
    "index_start": [
        "tool:close",
        "formation:stop",
        "resolver:clear",
        "audio:close",
        "router:clear",
        "inference:close",
    ],
    "consolidation_start": [
        "consolidation:stop",
        "index:stop",
        "formation:stop",
        "tool:close",
        "resolver:clear",
        "audio:close",
        "router:clear",
        "inference:close",
    ],
}


_ALL_CLEANUP_EVENTS = {
    "tool:close",
    "formation:stop",
    "index:stop",
    "consolidation:stop",
    "audio:close",
    "resolver:clear",
    "router:clear",
    "inference:close",
}


def _inject_inside_try_failure(
    monkeypatch: pytest.MonkeyPatch, events: list[str], boundary: str
) -> None:
    import app.livekit_transport.production as production
    import app.runtime.audio as audio_runtime
    import app.runtime.history as history_runtime
    import app.runtime.life as life_runtime
    import app.runtime.memory as memory_runtime
    import app.runtime.privacy as privacy_runtime
    import app.runtime.screen as screen_runtime
    import app.runtime.tools as tool_runtime_module
    from app import main

    failure = Mock(side_effect=_injected(boundary))
    if boundary == "screen_perception":
        monkeypatch.setattr(screen_runtime, "ScreenPerceptionService", failure)
    elif boundary == "semantic_classifier_client":
        monkeypatch.setattr(
            privacy_runtime, "InferenceSemanticClassifierClient", failure
        )
    elif boundary == "semantic_store":
        monkeypatch.setattr(memory_runtime, "SemanticStore", failure)
    elif boundary == "persona_memory_provider":
        monkeypatch.setattr(memory_runtime, "PersonaMemoryProvider", failure)
    elif boundary == "episodic_memory_management":
        monkeypatch.setattr(memory_runtime, "EpisodicMemoryManagement", failure)
    elif boundary == "addon_record_provider":
        monkeypatch.setattr(memory_runtime, "AddonRecordProvider", failure)
    elif boundary == "rag_admission_service":
        monkeypatch.setattr(memory_runtime, "RagAdmissionService", failure)
    elif boundary == "memory_inference_client":
        monkeypatch.setattr(
            memory_runtime, "StructuredMemoryInferenceClient", failure
        )
    elif boundary == "memory_candidate_extractor":
        monkeypatch.setattr(memory_runtime, "MemoryCandidateExtractor", failure)
    elif boundary == "preference_scheduler_build":
        monkeypatch.setattr(memory_runtime, "MemoryFormationScheduler", failure)
    elif boundary == "episodic_scheduler_build":
        monkeypatch.setattr(memory_runtime, "build_episodic_scheduler", failure)
    elif boundary == "formation_start":
        monkeypatch.setattr(
            memory_runtime,
            "CombinedFormationScheduler",
            _async_scheduler(
                "formation", events, fail_start=True, self_stop=True
            ),
        )
    elif boundary == "consolidation_probe":
        monkeypatch.setattr(memory_runtime, "ConsolidationPriorityProbe", failure)
    elif boundary == "consolidation_planner":
        monkeypatch.setattr(memory_runtime, "ConsolidationPlanner", failure)
    elif boundary == "conversation_history_service":
        monkeypatch.setattr(
            history_runtime, "ConversationHistoryService", failure
        )
    elif boundary == "life_settings":
        settings = Mock()
        settings.load.side_effect = _injected(boundary)
        monkeypatch.setattr(life_runtime, "LifeSettings", settings)
    elif boundary == "tool_construct":
        monkeypatch.setattr(tool_runtime_module, "ToolRuntime", failure)
    elif boundary == "tool_start":
        monkeypatch.setattr(
            tool_runtime_module,
            "ToolRuntime",
            _stub_tool_runtime(events, fail_start=True),
        )
    elif boundary == "chat_runtime_config":
        monkeypatch.setattr(
            main._chat_runtime, "resolve_chat_runtime_config", failure
        )
    elif boundary == "chat_service":
        monkeypatch.setattr(
            main._chat_runtime, "create_chat_service", failure
        )
    elif boundary == "audio_runtime_config":
        monkeypatch.setattr(audio_runtime, "resolve_audio_runtime_config", failure)
    elif boundary == "audio_pipeline_service":
        monkeypatch.setattr(
            audio_runtime, "create_audio_pipeline_service", failure
        )
    elif boundary == "configure_production":
        monkeypatch.setattr(
            production, "configure_production_resources", failure
        )
    elif boundary == "register_chat_resolver":
        monkeypatch.setattr(
            main._chat_runtime,
            "register_default_chat_service_resolver",
            failure,
        )
    elif boundary == "index_start":
        monkeypatch.setattr(
            memory_runtime,
            "MemoryIndexScheduler",
            _index_scheduler(events, fail_start=True),
        )
    elif boundary == "consolidation_start":
        monkeypatch.setattr(
            memory_runtime,
            "MemoryConsolidationScheduler",
            _async_scheduler(
                "consolidation", events, fail_start=True, self_stop=True
            ),
        )
    else:
        raise AssertionError(f"unknown boundary: {boundary}")


_RUNNING_STATE_NAMES = {
    "notifications",
    "notification_only",
    "inference_router",
    "inference_health",
    "voice_measurement_kind",
    "conversation_history_repository",
    "conversation_lifecycle_service",
    "ui_settings_repository",
    "screen_http_security",
    "screen_perception_service",
    "semantic_privacy_classifier",
    "semantic_memory_management",
    "semantic_store",
    "persona_memory_provider",
    "episodic_memory_management",
    "addon_record_provider",
    "rag_admission_service",
    "addon_manager",
    "event_source",
    "action_policy",
    "tool_service",
    "chat_service",
    "audio_pipeline_service",
}

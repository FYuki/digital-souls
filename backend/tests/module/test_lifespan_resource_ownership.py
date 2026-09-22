"""lifespanの資源所有・起動停止順・失敗時回収の契約を固定する。

#482でmain.pyのlifespanを資源所有単位へ分割する前に、現在の観測可能な
振る舞いを固定する。起動順・停止順・起動途中失敗時の回収・cleanup失敗時の
継続・process global登録の解除・optional無効・同一app再起動の各契約は、
分割後の実装でも維持されなければならない。
"""

from __future__ import annotations

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
        production,
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


class TestStartupAcquisitionOrder:
    def test_startup_acquires_owners_in_dependency_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main

        events: list[str] = []
        _install_owner_stubs(monkeypatch, events)
        _spy_inference_router_registration(monkeypatch, events)
        _spy_chat_resolver_registration(monkeypatch, events)
        _spy_chat_service_create(monkeypatch, events)

        with TestClient(main.app):
            pass

        _assert_event_subsequence(
            events,
            [
                "router:register",
                "formation:start",
                "tool:start",
                "chat:create",
                "audio:create",
                "resolver:register",
                "index:start",
                "consolidation:start",
            ],
        )

    def test_startup_acquires_life_and_livekit_in_dependency_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main

        import app.runtime.life as life_runtime

        events: list[str] = []
        _install_owner_stubs(monkeypatch, events)
        monkeypatch.setattr(
            life_runtime, "LifeRuntime", _life_runtime(events)
        )
        _install_livekit_stubs(monkeypatch, events, fail_configure=False)
        _enable_character_life(monkeypatch)
        _spy_chat_service_create(monkeypatch, events)

        with TestClient(main.app):
            pass

        _assert_event_subsequence(
            events,
            [
                "formation:start",
                "tool:start",
                "life:start",
                "chat:create",
                "audio:create",
                "livekit:configure",
                "index:start",
                "consolidation:start",
            ],
        )


class TestShutdownCollectionOrder:
    def test_shutdown_collects_owned_resources_once_in_dependency_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main

        events: list[str] = []
        maintenance_leases: list[Any] = []
        _install_owner_stubs(monkeypatch, events)
        _record_inference_close(monkeypatch, events)
        _record_lease_close(monkeypatch, events)
        _capture_maintenance_lease(monkeypatch, maintenance_leases)

        with TestClient(main.app):
            pass

        _assert_event_subsequence(
            events,
            [
                "tool:close",
                "consolidation:stop",
                "formation:stop",
                "index:stop",
                "inference:close",
                "audio:close",
            ],
        )
        assert len(maintenance_leases) == 1
        # maintenance leaseは全資源の回収後まで保持される。
        assert events[-1] == f"lease-close:{id(maintenance_leases[0])}"
        assert events.index("inference:close") < len(events) - 1

    def test_shutdown_with_life_and_livekit_collects_in_dependency_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main

        import app.runtime.life as life_runtime

        events: list[str] = []
        maintenance_leases: list[Any] = []
        _install_owner_stubs(monkeypatch, events)
        monkeypatch.setattr(
            life_runtime, "LifeRuntime", _life_runtime(events)
        )
        _install_livekit_stubs(monkeypatch, events, fail_configure=False)
        _enable_character_life(monkeypatch)
        _record_inference_close(monkeypatch, events)
        _record_lease_close(monkeypatch, events)
        _capture_maintenance_lease(monkeypatch, maintenance_leases)

        with TestClient(main.app):
            pass

        _assert_event_subsequence(
            events,
            [
                "livekit:cancel_preparations",
                "livekit:stop_all",
                "livekit:aclose",
                "life:close",
                "tool:close",
                "consolidation:stop",
                "formation:stop",
                "index:stop",
                "inference:close",
                "transcriber:close",
                "synthesizer:close",
                "audio:close",
            ],
        )
        assert events[-1] == f"lease-close:{id(maintenance_leases[0])}"
        assert not hasattr(main.app.state, "character_life_runtime")
        assert not hasattr(main.app.state, "livekit_room_manager")


class TestProbeFailure:
    @pytest.mark.anyio
    async def test_probe_failure_closes_inference_before_other_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main
        from app.inference.runtime import InferenceRuntime

        events: list[str] = []
        _record_inference_close(monkeypatch, events)

        def probe(_runtime: InferenceRuntime) -> None:
            raise _injected("probe")

        import app.runtime.history as history_runtime

        monkeypatch.setattr(InferenceRuntime, "probe_startup", probe)
        repository = Mock(side_effect=AssertionError("must not construct"))
        monkeypatch.setattr(
            history_runtime, "ConversationHistoryRepository", repository
        )

        with pytest.raises(ValueError, match="injected probe"):
            async with main.lifespan(FastAPI()):
                pytest.fail("probe failure must prevent startup")

        assert events.count("inference:close") == 1
        repository.assert_not_called()


# 各失敗点で「それ以前に確保・起動した資源だけが一度だけ回収される」ことを検証する。
# present: その失敗点で実行されるべき回収イベント。後続資源は回収されない。
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


class TestStartupFailureInsideProtectedRegion:
    """主要try内の各所有境界の失敗で、確保済み資源が必要な順に一度だけ回収される。"""

    @pytest.mark.anyio
    @pytest.mark.parametrize("boundary", sorted(_INSIDE_TRY_EXPECTED))
    async def test_failure_collects_each_acquired_owner_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        conversation_history_database_path: Any,
        boundary: str,
    ) -> None:
        import app.chat_service as chat_service
        from app import main
        from app.conversation_history.sqlite_lease import (
            acquire_maintenance_lease,
        )

        events: list[str] = []
        _install_owner_stubs(monkeypatch, events)
        _record_inference_close(monkeypatch, events)
        _spy_inference_router_registration(monkeypatch, events)
        _spy_chat_resolver_registration(monkeypatch, events)
        _inject_inside_try_failure(monkeypatch, events, boundary)

        app = FastAPI()
        with pytest.raises(ValueError, match="injected"):
            async with main.lifespan(app):
                pytest.fail("injected failure must prevent startup")

        expected = _INSIDE_TRY_EXPECTED[boundary]
        for name in expected:
            assert events.count(name) == 1, (
                f"{boundary}: expected one {name}, events={events}"
            )
        for name in _ALL_CLEANUP_EVENTS - set(expected) - {
            "router:register",
            "resolver:register",
        }:
            assert events.count(name) == 0, (
                f"{boundary}: unexpected {name}, events={events}"
            )
        # 起動に使った global登録・公開状態・SQLite leaseを残さない。
        for state_name in (
            "inference_router",
            "inference_health",
            "conversation_history_repository",
            "chat_service",
            "audio_pipeline_service",
        ):
            assert not hasattr(app.state, state_name), boundary
        with acquire_maintenance_lease(conversation_history_database_path):
            pass
        with pytest.raises(chat_service.ChatServiceError):
            chat_service.generate_chat_reply("miori", CONVERSATION_ID, "hello")


class TestStartupFailureResourceGaps:
    """失敗点で公開・確保した資源が回収されない経路の再現検証。"""


    @pytest.mark.anyio
    async def test_enabled_life_requires_target_and_collects_prior_owners(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main

        events: list[str] = []
        _install_owner_stubs(monkeypatch, events)
        _record_inference_close(monkeypatch, events)
        monkeypatch.setenv("DS_CHARACTER_LIFE_ENABLED", "true")

        with pytest.raises(ValueError, match="Character Life requires"):
            async with main.lifespan(FastAPI()):
                pytest.fail("missing life target must prevent startup")

        assert events.count("formation:stop") == 1
        assert events.count("inference:close") == 1
        assert events.count("tool:close") == 0

    @pytest.mark.anyio
    async def test_livekit_configure_failure_collects_audio_and_prior_owners(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main

        events: list[str] = []
        _install_owner_stubs(monkeypatch, events)
        _install_livekit_stubs(monkeypatch, events, fail_configure=True)
        _record_inference_close(monkeypatch, events)

        app = FastAPI()
        with pytest.raises(ValueError, match="injected livekit configure"):
            async with main.lifespan(app):
                pytest.fail("livekit configure failure must prevent startup")

        _assert_event_subsequence(
            events,
            [
                "tool:close",
                "formation:stop",
                "inference:close",
                "transcriber:close",
                "synthesizer:close",
                "audio:close",
            ],
        )
        assert events.count("index:stop") == 0
        assert not hasattr(app.state, "livekit_room_manager")

    @pytest.mark.anyio
    async def test_lifespan_recovers_after_startup_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.runtime.tools as tool_runtime_module
        from app import main

        tool_runtimes = iter(
            [
                _stub_tool_runtime([], fail_start=True),
                _stub_tool_runtime([]),
            ]
        )
        monkeypatch.setattr(
            tool_runtime_module,
            "ToolRuntime",
            lambda *_args, **_kwargs: next(tool_runtimes)(),
        )

        with pytest.raises(ValueError, match="injected tool start"):
            with TestClient(main.app):
                pytest.fail("tool start failure must prevent startup")

        # global登録と資源が回収されているため、同じappで再度起動できる。
        with TestClient(main.app):
            assert hasattr(main.app.state, "tool_service")


class TestCleanupFailureContinuation:
    def test_consolidation_stop_failure_continues_remaining_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main

        events: list[str] = []
        _install_owner_stubs(
            monkeypatch,
            events,
            consolidation=_async_scheduler(
                "consolidation", events, fail_stop=True
            ),
        )
        _record_inference_close(monkeypatch, events)

        with pytest.raises(ValueError, match="injected consolidation stop"):
            with TestClient(main.app):
                pass

        _assert_event_subsequence(
            events,
            [
                "tool:close",
                "consolidation:stop",
                "formation:stop",
                "index:stop",
                "inference:close",
                "audio:close",
            ],
        )

    def test_tool_close_failure_continues_remaining_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main

        events: list[str] = []
        _install_owner_stubs(
            monkeypatch,
            events,
            tool=_stub_tool_runtime(events, fail_close=True),
        )
        _record_inference_close(monkeypatch, events)

        with pytest.raises(ValueError, match="injected tool close"):
            with TestClient(main.app):
                pass

        _assert_event_subsequence(
            events,
            [
                "tool:close",
                "consolidation:stop",
                "formation:stop",
                "index:stop",
                "inference:close",
                "audio:close",
            ],
        )

    def test_first_async_cleanup_error_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main

        events: list[str] = []
        _install_owner_stubs(
            monkeypatch,
            events,
            tool=_stub_tool_runtime(events, fail_close=True),
            consolidation=_async_scheduler(
                "consolidation", events, fail_stop=True
            ),
        )
        _record_inference_close(monkeypatch, events)

        # tool closeが先に失敗するため、先に集約した例外を外へ返す。
        with pytest.raises(ValueError, match="injected tool close"):
            with TestClient(main.app):
                pass

        _assert_event_subsequence(
            events,
            [
                "tool:close",
                "consolidation:stop",
                "formation:stop",
                "index:stop",
                "inference:close",
                "audio:close",
            ],
        )

    def test_exitstack_cleanup_error_propagates_instead_of_async_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main

        events: list[str] = []
        _install_owner_stubs(
            monkeypatch,
            events,
            tool=_stub_tool_runtime(events, fail_close=True),
            audio=_audio_service(events, fail_close=True),
        )
        _record_inference_close(monkeypatch, events)

        # 非同期cleanupの失敗を集約したあと、ExitStack側の失敗が外へ出る。
        with pytest.raises(ValueError, match="injected audio close"):
            with TestClient(main.app):
                pass

        _assert_event_subsequence(
            events,
            [
                "tool:close",
                "consolidation:stop",
                "formation:stop",
                "index:stop",
                "inference:close",
                "audio:close",
            ],
        )


_RUNNING_STATE_NAMES = {
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


class TestStatePublication:
    def test_running_state_contract_is_published_and_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main

        with TestClient(main.app):
            assert set(main.app.state._state) == _RUNNING_STATE_NAMES
            assert main.app.state.tool_service is None
            assert main.app.state.voice_measurement_kind == "automated_test"
            assert main.app.state.event_source is None
        # addon_managerとevent_sourceは現行では停止後もstateに残る。
        # それ以外の公開名はすべて回収される。
        assert set(main.app.state._state) <= {"addon_manager", "event_source"}
        for name in _RUNNING_STATE_NAMES - {"addon_manager", "event_source"}:
            assert not hasattr(main.app.state, name)


class TestOptionalResources:
    def test_disabled_character_life_is_not_constructed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.runtime.life as life_runtime
        from app import main

        constructed = []
        for name in (
            "LifeStore",
            "LifeService",
            "LifeRuntime",
            "LifeContext",
            "LifeFormation",
            "CharacterCatalog",
        ):
            spy = Mock(side_effect=AssertionError(f"{name} must not run"))
            monkeypatch.setattr(life_runtime, name, spy)
            constructed.append(spy)

        with TestClient(main.app):
            assert not hasattr(main.app.state, "character_life_runtime")

        for spy in constructed:
            spy.assert_not_called()

    def test_disabled_livekit_is_not_constructed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.livekit_transport.production as production
        from app import main

        # Core session factoryはLiveKit設定があるときだけ構築される。
        # 構築されなければCore用のSTT/TTS clientやcallbackも生成されない。
        session_factory = Mock(side_effect=AssertionError("must not construct"))
        monkeypatch.setattr(
            production,
            "ProductionConversationCoreSessionFactory",
            session_factory,
        )
        configure = Mock(wraps=production.configure_production_resources)
        monkeypatch.setattr(
            production, "configure_production_resources", configure
        )

        with TestClient(main.app):
            for name in (
                "livekit_room_manager",
                "livekit_session_repository",
                "livekit_runtime_manager",
                "livekit_token_signer",
                "livekit_bootstrap_service",
                "livekit_core_events",
                "livekit_url",
            ):
                assert not hasattr(main.app.state, name)

        configure.assert_called_once()
        session_factory.assert_not_called()

    def test_disabled_memory_formation_uses_disabled_scheduler(
        self, monkeypatch: pytest.MonkeyPatch, runtime_paths: Any
    ) -> None:
        from app import main

        monkeypatch.setenv("VOICE_MEASUREMENT_DISABLE_MEMORY_FORMATION", "true")
        monkeypatch.setenv("VOICE_MEASUREMENT_KIND", "controlled_baseline")
        import app.runtime.memory as memory_runtime

        monkeypatch.setenv(
            "VOICE_CONTROLLED_TRACE_PATH",
            str(runtime_paths.data_root / "controlled.jsonl"),
        )
        disabled_scheduler = Mock(wraps=memory_runtime.DisabledFormationScheduler)
        monkeypatch.setattr(
            memory_runtime, "DisabledFormationScheduler", disabled_scheduler
        )
        unused = []
        for name in (
            "CombinedFormationScheduler",
            "MemoryFormationScheduler",
            "build_semantic_scheduler",
            "build_episodic_scheduler",
        ):
            spy = Mock(side_effect=AssertionError(f"{name} must not run"))
            monkeypatch.setattr(memory_runtime, name, spy)
            unused.append(spy)
        events: list[str] = []
        consolidation = _async_scheduler("consolidation", events)
        monkeypatch.setattr(
            memory_runtime, "MemoryConsolidationScheduler", consolidation
        )

        with TestClient(main.app):
            pass

        disabled_scheduler.assert_called_once()
        for spy in unused:
            spy.assert_not_called()
        assert len(consolidation.instances) == 1
        assert consolidation.instances[0].start_calls == 0


class TestProcessGlobalRegistration:
    def test_inference_router_registration_is_paired_and_scoped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main
        from app.llm.router import current_inference_router

        registered: list[Any] = []
        cleared: list[Any] = []
        register = main.llm_router.register_inference_router
        clear = main.llm_router.clear_inference_router

        def record_register(router: Any) -> None:
            registered.append(router)
            register(router)

        def record_clear(router: Any) -> None:
            cleared.append(router)
            clear(router)

        monkeypatch.setattr(
            main.llm_router, "register_inference_router", record_register
        )
        monkeypatch.setattr(
            main.llm_router, "clear_inference_router", record_clear
        )

        with TestClient(main.app):
            assert registered == [main.app.state.inference_router]
            assert current_inference_router() is registered[0]

        assert cleared == registered
        assert current_inference_router() is None

    def test_chat_service_resolver_registration_is_paired_and_scoped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.chat_service as chat_service
        from app import main

        registered: list[Any] = []
        cleared: list[Any] = []
        register = main._chat_runtime.register_default_chat_service_resolver
        clear = main._chat_runtime.clear_default_chat_service_resolver

        def record_register(resolver: Any) -> None:
            registered.append(resolver)
            register(resolver)

        def record_clear(resolver: Any) -> None:
            cleared.append(resolver)
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

        with TestClient(main.app):
            assert len(registered) == 1
            assert registered[0]() is main.app.state.chat_service

        assert cleared == registered
        with pytest.raises(chat_service.ChatServiceError):
            chat_service.generate_chat_reply("miori", CONVERSATION_ID, "hello")


class TestSameAppRestart:
    def test_second_lifespan_replaces_owned_resources_and_registrations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.chat_service as chat_service
        from app import main
        from app.llm.router import current_inference_router

        with TestClient(main.app):
            first_router = main.app.state.inference_router
            first_service = main.app.state.chat_service
            first_manager = main.app.state.addon_manager
            first_repository = main.app.state.conversation_history_repository
            assert current_inference_router() is first_router

        assert current_inference_router() is None
        assert not hasattr(main.app.state, "inference_router")
        with pytest.raises(chat_service.ChatServiceError):
            chat_service.generate_chat_reply("miori", CONVERSATION_ID, "hello")

        # 同一FastAPI appで2回目のlifespanを実行し、古い登録・資源を再利用しない。
        with TestClient(main.app):
            second_router = main.app.state.inference_router
            assert second_router is not first_router
            assert current_inference_router() is second_router
            assert main.app.state.chat_service is not first_service
            assert main.app.state.addon_manager is not first_manager
            assert (
                main.app.state.conversation_history_repository
                is not first_repository
            )

        assert current_inference_router() is None

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


from tests.lifespan_ownership_test_support import (
    _injected,
    _assert_event_subsequence,
    _record_inference_close,
    _record_lease_close,
    _capture_maintenance_lease,
    _spy_inference_router_registration,
    _spy_chat_resolver_registration,
    _spy_chat_service_create,
    _stub_tool_runtime,
    _async_scheduler,
    _index_scheduler,
    _audio_service,
    _life_runtime,
    _install_owner_stubs,
    _install_livekit_stubs,
    _enable_character_life,
    _PRE_TRY_BOUNDARIES,
    _PRE_TRY_MODULES,
    _inject_pre_try_failure,
    _INSIDE_TRY_EXPECTED,
    _ALL_CLEANUP_EVENTS,
    _inject_inside_try_failure,
    _RUNNING_STATE_NAMES,
)

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
    async def test_life_start_failure_collects_runtime_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.runtime.life as life_runtime
        from app import main

        events: list[str] = []
        _install_owner_stubs(monkeypatch, events)
        _record_inference_close(monkeypatch, events)
        monkeypatch.setattr(
            life_runtime, "LifeRuntime", _life_runtime(events, fail_start=True)
        )
        _enable_character_life(monkeypatch)

        with pytest.raises(ValueError, match="injected life start"):
            async with main.lifespan(FastAPI()):
                pytest.fail("life start failure must prevent startup")

        # start()内の自己回収と上位cleanupが同じruntimeを二度回収しない。
        assert events.count("life:close") == 1
        assert events.count("tool:close") == 1
        assert events.count("formation:stop") == 1
        assert events.count("inference:close") == 1

    @pytest.mark.anyio
    async def test_screen_perception_failure_removes_published_security_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app import main

        import app.runtime.screen as screen_runtime

        _install_owner_stubs(monkeypatch, [])
        monkeypatch.setattr(
            screen_runtime,
            "ScreenPerceptionService",
            Mock(side_effect=_injected("screen perception")),
        )

        app = FastAPI()
        with pytest.raises(ValueError, match="injected screen perception"):
            async with main.lifespan(app):
                pytest.fail("screen perception failure must prevent startup")

        assert not hasattr(app.state, "screen_http_security")
        assert not hasattr(app.state, "screen_perception_service")

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
        # mainの通知統合後は資源を回収し、縮退モードのfalseだけを保持する。
        assert main.app.state._state == {"notification_only": False}
        for name in _RUNNING_STATE_NAMES - {"notification_only"}:
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
        import app.livekit_transport.core_factory as core_factory
        import app.livekit_transport.production as production
        from app import main

        # Core session factoryはLiveKit設定があるときだけ構築される。
        # 構築されなければCore用のSTT/TTS clientやcallbackも生成されない。
        session_factory = Mock(side_effect=AssertionError("must not construct"))
        monkeypatch.setattr(
            core_factory,
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

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


class TestStartupFailureBeforeProtectedRegion:
    """推論生成後から主要try/finally以前の失敗でも確保済み資源を回収する。

    inference runtimeは主要tryより前に生成されるため、この区間の失敗でも
    確保済みのadapterが一度だけ解放される必要がある。
    """

    @pytest.mark.anyio
    @pytest.mark.parametrize("boundary", _PRE_TRY_BOUNDARIES)
    async def test_failure_releases_acquired_resources_once(
        self, monkeypatch: pytest.MonkeyPatch, boundary: str
    ) -> None:
        from app import main

        events: list[str] = []
        _record_inference_close(monkeypatch, events)
        _inject_pre_try_failure(monkeypatch, boundary)

        app = FastAPI()
        with pytest.raises(ValueError, match="injected"):
            async with main.lifespan(app):
                pytest.fail("injected failure must prevent startup")

        assert events.count("inference:close") == 1

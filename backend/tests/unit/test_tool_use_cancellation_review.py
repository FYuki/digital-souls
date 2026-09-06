"""中断・失敗の後始末で元の制御フローを壊さない回帰。"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import _chat_runtime
from app.chat_service import ChatBackendError, ChatTimeoutError
from app.external_mcp.models import MCPFailure
from app.routers import chat as route
from app.tool_use.routing import parse_object
from tests.unit.test_tool_use import Decisions, runtime
from tests.unit.test_conversation_core_lifecycle import _session, UTTERANCE_1


def _failing_chat(monkeypatch, error, fail_turn):
    service = object.__new__(_chat_runtime.ChatService)
    service._runtime_config = None
    service._dependencies = None
    service.tools = SimpleNamespace(stop=lambda *_: None)
    history = SimpleNamespace(start_turn=lambda *_: "started", fail_turn=fail_turn)
    service._conversation_history_service = SimpleNamespace(
        open_session=lambda *_: history
    )
    monkeypatch.setattr(_chat_runtime, "_resolve_chat_context", lambda *_: None)

    def fail(*_):
        raise error

    service.prepare_unrecorded_generation = fail
    return service


@pytest.mark.parametrize("error", [ChatBackendError(), ChatTimeoutError()])
def test_failed_history_cleanup_preserves_original_chat_error(monkeypatch, error):
    def cleanup(_):
        raise RuntimeError("履歴保存の失敗")

    service = _failing_chat(monkeypatch, error, cleanup)
    with pytest.raises(type(error)) as raised:
        asyncio.run(service._generate_tool_reply("miori", uuid4(), "入力", None, None))
    assert raised.value is error


def test_additional_cancellation_waits_for_history_cleanup(monkeypatch):
    async def exercise():
        cleanup_started, release = asyncio.Event(), asyncio.Event()
        original = asyncio.CancelledError("original")
        service = _failing_chat(monkeypatch, original, lambda _: None)
        original_run_sync = _chat_runtime.run_sync
        finished = []

        async def worker(function, *args):
            if (
                function
                is service._conversation_history_service.open_session().fail_turn
            ):
                cleanup_started.set()
                await release.wait()
                finished.append(True)
                return None
            return await original_run_sync(function, *args)

        monkeypatch.setattr(_chat_runtime, "run_sync", worker)
        task = asyncio.create_task(
            service._generate_tool_reply("miori", uuid4(), "入力", None, None)
        )
        await asyncio.wait_for(cleanup_started.wait(), 2)
        task.cancel("additional")
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert raised.value is original
        assert finished == [True]

    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["stop", "disconnect", "shutdown", "close"])
def test_http_cancellation_only_converts_explicit_stop_and_disconnect(
    monkeypatch, action
):
    async def exercise():
        async with runtime(Decisions()) as (service, _source, _gate):
            started, disconnected = asyncio.Event(), asyncio.Event()
            payload = route.ChatRequest(
                character="miori", conversation_id=uuid4(), message="入力"
            )

            async def response(*_):
                with service.response_scope(
                    payload.character, str(payload.conversation_id)
                ):
                    started.set()
                    await asyncio.Event().wait()

            async def is_disconnected():
                return disconnected.is_set()

            monkeypatch.setattr(route, "_chat_response", response)
            request = SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(tool_service=service)),
                is_disconnected=is_disconnected,
            )
            task = asyncio.create_task(route.chat(payload, request))
            await started.wait()
            if action == "stop":
                service.stop(payload.character, str(payload.conversation_id))
            elif action == "disconnect":
                disconnected.set()
            elif action == "close":
                service.close()
            else:
                task.cancel("shutdown")
            if action in {"stop", "disconnect"}:
                assert (await asyncio.wait_for(task, 2)).status_code == 499
            else:
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert not service._owners

    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["end", "disconnect", "cancel_response"])
def test_core_terminal_cleanup_survives_interruption_callback_error(action):
    async def exercise():
        module, session, _delivery, persistence, _observation = _session()

        def fail(_):
            raise RuntimeError("外部停止の失敗")

        session._on_interruption = fail
        response = await session.finalize_utterance(
            utterance_id=UTTERANCE_1, transcript="入力", should_response=True
        )
        if action == "cancel_response":
            await session.cancel_response(
                response_id=response.response_id, reason="barge_in"
            )
        else:
            await getattr(session, action)()
        assert (
            session.response(response.response_id).state
            is module.ResponseState.CANCELLED
        )
        assert session.active_response is None
        assert session.running_stage_count == 0
        assert len(persistence.outcomes) == 1

    asyncio.run(exercise())


def test_deep_decision_json_is_a_validation_failure(monkeypatch):
    text = '{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}"
    assert len(text) < 16_384
    # C版decoderの再帰上限はPython buildごとに異なるため標準Python版で再現する。
    decoder = json.JSONDecoder()
    decoder.scan_once = json.scanner.py_make_scanner(decoder)
    monkeypatch.setattr(json, "loads", decoder.decode)
    with pytest.raises(MCPFailure, match="invalid_decision"):
        parse_object(text)

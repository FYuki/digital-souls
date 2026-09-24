from __future__ import annotations

import asyncio
import importlib

import pytest


def _task_tracker_type():
    try:
        module = importlib.import_module("app.conversation_core.task_tracker")
    except ModuleNotFoundError as error:
        if error.name == "app.conversation_core.task_tracker":
            pytest.fail("Conversation Core task tracker boundary is not implemented")
        raise
    return module.TaskTracker


def test_task_tracker_cancels_only_the_requested_response_stages():
    async def scenario() -> None:
        tracker = _task_tracker_type()()
        cancelled: list[str] = []

        async def wait_for_cancel(name: str) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        first = tracker.register_stage(
            wait_for_cancel("first"), response_key=("response-1", 1)
        )
        second = tracker.register_stage(
            wait_for_cancel("second"), response_key=("response-2", 1)
        )
        await asyncio.sleep(0)
        await tracker.cancel_response("response-1", 1)

        assert first.done()
        assert not second.done()
        assert cancelled == ["first"]

        await tracker.cancel_all_stages()
        assert cancelled == ["first", "second"]

    asyncio.run(scenario())


def test_task_tracker_does_not_wait_for_the_effect_that_is_draining_itself():
    async def scenario() -> None:
        tracker = _task_tracker_type()()
        drained = asyncio.Event()

        async def effect() -> None:
            await tracker.finish_effects()
            drained.set()

        task = tracker.register_effect(effect())
        await asyncio.wait_for(task, 1)

        assert drained.is_set()

    asyncio.run(scenario())


def test_task_tracker_removes_done_tasks_and_response_keys_together():
    async def scenario() -> None:
        tracker = _task_tracker_type()()

        async def complete() -> None:
            return None

        task = tracker.register_stage(complete(), response_key=("response-1", 1))
        await task
        await asyncio.sleep(0)

        assert tracker.stage_tasks == ()
        assert tracker.stage_task_keys == {}

    asyncio.run(scenario())

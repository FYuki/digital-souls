from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Callable
from typing import TypeVar


TaskResult = TypeVar("TaskResult")
TaskKey = tuple[str, int]


class TaskTracker:
    """Coreのstage/effect taskの所有、応答単位取消、終了待ちを担う。"""

    def __init__(
        self,
        *,
        on_unhandled_error: Callable[[asyncio.Task[object]], None] | None = None,
    ) -> None:
        self._stage_tasks: set[asyncio.Task[object]] = set()
        self._stage_task_keys: dict[asyncio.Task[object], TaskKey] = {}
        self._effect_tasks: set[asyncio.Task[object]] = set()
        self._on_unhandled_error = on_unhandled_error

    @property
    def stage_tasks(self) -> tuple[asyncio.Task[object], ...]:
        return tuple(self._stage_tasks)

    @property
    def stage_task_keys(self) -> dict[asyncio.Task[object], TaskKey]:
        return dict(self._stage_task_keys)

    @property
    def effect_tasks(self) -> tuple[asyncio.Task[object], ...]:
        return tuple(self._effect_tasks)

    @property
    def running_task_count(self) -> int:
        return sum(
            not task.done() for task in self._stage_tasks | self._effect_tasks
        )

    def register_stage(
        self,
        operation: Coroutine[object, object, TaskResult],
        *,
        response_key: TaskKey | None = None,
    ) -> asyncio.Task[TaskResult]:
        task = asyncio.create_task(operation)
        self._stage_tasks.add(task)
        if response_key is not None:
            self._stage_task_keys[task] = response_key
        task.add_done_callback(self._forget_stage)
        return task

    def register_effect(
        self, operation: Coroutine[object, object, TaskResult]
    ) -> asyncio.Task[TaskResult]:
        task = asyncio.create_task(operation)
        self._effect_tasks.add(task)
        task.add_done_callback(self._forget_effect)
        return task

    def tasks_for_response(self, response_key: TaskKey) -> tuple[asyncio.Task[object], ...]:
        return tuple(
            task
            for task, key in self._stage_task_keys.items()
            if key == response_key
        )

    def request_response_cancellation(self, response_key: TaskKey) -> None:
        current = asyncio.current_task()
        for task in self.tasks_for_response(response_key):
            if task is not current:
                task.cancel()

    async def cancel_response(self, response_id: str, generation: int) -> None:
        await self._cancel_tasks(self.tasks_for_response((response_id, generation)))

    async def cancel_all_stages(self) -> None:
        await self._cancel_tasks(tuple(self._stage_tasks))

    async def finish_effects(self) -> None:
        current = asyncio.current_task()
        tasks = tuple(task for task in self._effect_tasks if task is not current)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_tasks(self, tasks: tuple[asyncio.Task[object], ...]) -> None:
        current = asyncio.current_task()
        cancellable = tuple(task for task in tasks if task is not current)
        for task in cancellable:
            task.cancel()
        if cancellable:
            await asyncio.gather(*cancellable, return_exceptions=True)

    def _forget_stage(self, task: asyncio.Task[object]) -> None:
        self._stage_tasks.discard(task)
        self._stage_task_keys.pop(task, None)
        self._report_unhandled_error(task)

    def _forget_effect(self, task: asyncio.Task[object]) -> None:
        self._effect_tasks.discard(task)
        self._report_unhandled_error(task)

    def _report_unhandled_error(self, task: asyncio.Task[object]) -> None:
        if self._on_unhandled_error is not None:
            self._on_unhandled_error(task)

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.memory.formation.contracts import MemoryFormationJob

logger = logging.getLogger(__name__)


class FormationWorker(Protocol):
    def process(self, job: MemoryFormationJob) -> None: ...


@dataclass(frozen=True)
class _QueuedJob:
    job: MemoryFormationJob
    submitted_at: float


class MemoryFormationScheduler:
    def __init__(
        self,
        *,
        worker: FormationWorker,
        max_queue_age_seconds: int,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._worker = worker
        self._max_queue_age_seconds = max_queue_age_seconds
        self._clock = monotonic_clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_QueuedJob | None] | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("memory formation scheduler is already running")
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._run())

    def submit(self, job: MemoryFormationJob) -> None:
        if self._loop is None or self._queue is None or self._task is None:
            raise RuntimeError("memory formation scheduler is not running")
        queued = _QueuedJob(job, self._clock())
        self._loop.call_soon_threadsafe(self._queue.put_nowait, queued)

    async def stop(self) -> None:
        if self._task is None or self._queue is None:
            return
        self._queue.put_nowait(None)
        try:
            await self._task
        finally:
            self._task = None
            self._queue = None
            self._loop = None

    async def _run(self) -> None:
        queue = self._queue
        if queue is None:
            raise RuntimeError("memory formation queue is not initialized")
        while True:
            queued = await queue.get()
            if queued is None:
                return
            if self._clock() - queued.submitted_at > self._max_queue_age_seconds:
                logger.info("memory formation job expired")
                continue
            await asyncio.to_thread(self._worker.process, queued.job)

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
        queue_maxsize: int,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if queue_maxsize < 1:
            raise ValueError("memory formation queue maxsize must be positive")
        self._worker = worker
        self._max_queue_age_seconds = max_queue_age_seconds
        self._queue_maxsize = queue_maxsize
        self._clock = monotonic_clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_QueuedJob | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._accepting_submissions = False

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("memory formation scheduler is already running")
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self._queue_maxsize)
        self._task = asyncio.create_task(self._run())
        self._accepting_submissions = True

    def submit(self, job: MemoryFormationJob) -> None:
        if (
            not self._accepting_submissions
            or self._loop is None
            or self._queue is None
            or self._task is None
        ):
            raise RuntimeError("memory formation scheduler is not running")
        queued = _QueuedJob(job, self._clock())
        self._loop.call_soon_threadsafe(self._enqueue_or_drop, self._queue, queued)

    def _enqueue_or_drop(
        self, queue: asyncio.Queue[_QueuedJob | None], queued: _QueuedJob
    ) -> None:
        """満杯時は新しいjobを破棄し、会話経路を待機させない。"""
        if not self._accepting_submissions or queue is not self._queue:
            logger.info("memory formation job dropped: reason=shutdown")
            return
        try:
            queue.put_nowait(queued)
        except asyncio.QueueFull:
            logger.warning(
                "memory formation job dropped: reason=queue_full "
                "character_id=%s conversation_id=%s turn_id=%s",
                queued.job.character_id,
                queued.job.conversation_id,
                queued.job.turn_id,
            )

    async def stop(self) -> None:
        if self._task is None or self._queue is None:
            return
        self._accepting_submissions = False
        discarded = 0
        while True:
            try:
                queued = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if queued is not None:
                discarded += 1
        if discarded:
            logger.info(
                "memory formation jobs discarded on shutdown: count=%d",
                discarded,
            )
        self._queue.put_nowait(None)
        try:
            await self._task
        finally:
            self._accepting_submissions = False
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
            try:
                await asyncio.to_thread(self._worker.process, queued.job)
            except Exception as error:
                logger.warning(
                    "memory formation job raised: error_type=%s "
                    "character_id=%s conversation_id=%s turn_id=%s",
                    type(error).__name__,
                    queued.job.character_id,
                    queued.job.conversation_id,
                    queued.job.turn_id,
                )

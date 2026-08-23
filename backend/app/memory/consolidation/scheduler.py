from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConsolidationPriorityState:
    latest_conversation_activity_at: datetime | None
    processing_conversation_count: int
    formation_busy: bool
    pending_outbox_count: int


class ConsolidationService(Protocol):
    def run_once(
        self, *, deadline: float, should_stop: Callable[[], bool]
    ) -> None: ...


class ConversationPrioritySource(Protocol):
    def consolidation_activity(self) -> tuple[int, datetime | None]: ...


class FormationPrioritySource(Protocol):
    def is_busy(self) -> bool: ...


class OutboxPrioritySource(Protocol):
    def status_counts(self) -> tuple[int, int]: ...


class ConsolidationPriorityProbe:
    def __init__(
        self,
        *,
        conversation_repository: ConversationPrioritySource,
        formation_scheduler: FormationPrioritySource,
        outbox_repository: OutboxPrioritySource,
    ) -> None:
        self._conversation_repository = conversation_repository
        self._formation_scheduler = formation_scheduler
        self._outbox_repository = outbox_repository

    def read(self) -> ConsolidationPriorityState:
        processing_count, latest_activity = (
            self._conversation_repository.consolidation_activity()
        )
        pending, failed = self._outbox_repository.status_counts()
        return ConsolidationPriorityState(
            latest_conversation_activity_at=latest_activity,
            processing_conversation_count=processing_count,
            formation_busy=self._formation_scheduler.is_busy(),
            pending_outbox_count=pending + failed,
        )


def is_consolidation_eligible(
    *,
    now: datetime,
    priority: ConsolidationPriorityState,
    idle_seconds: int,
    nightly_start_hour: int,
    nightly_end_hour: int,
) -> bool:
    if (
        priority.processing_conversation_count
        or priority.formation_busy
        or priority.pending_outbox_count
    ):
        return False
    nightly = (
        nightly_start_hour <= now.hour < nightly_end_hour
        if nightly_start_hour <= nightly_end_hour
        else now.hour >= nightly_start_hour or now.hour < nightly_end_hour
    )
    latest = priority.latest_conversation_activity_at
    idle = latest is None or (now - latest).total_seconds() >= idle_seconds
    return nightly or idle


class MemoryConsolidationScheduler:
    def __init__(
        self,
        *,
        service: ConsolidationService,
        interval_seconds: int,
        max_runtime_seconds: int,
        priority_probe: Callable[[], bool],
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._interval_seconds = interval_seconds
        self._max_runtime_seconds = max_runtime_seconds
        self._priority_probe = priority_probe
        self._monotonic_clock = monotonic_clock
        self._periodic_task: asyncio.Task[None] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._periodic_task is not None:
            raise RuntimeError("memory consolidation scheduler is already running")
        self._stopping = False
        self._periodic_task = asyncio.create_task(self._run_periodically())
        try:
            await self.start_if_eligible()
        except BaseException:
            await self.stop()
            raise

    async def start_if_eligible(self) -> None:
        if self._periodic_task is None or self._stopping:
            raise RuntimeError("memory consolidation scheduler is not running")
        if self._worker_task is not None and not self._worker_task.done():
            await asyncio.wait((self._worker_task,), timeout=0.01)
            if not self._worker_task.done():
                return
        if not self._priority_probe():
            return
        deadline = self._monotonic_clock() + self._max_runtime_seconds
        self._worker_task = asyncio.create_task(self._execute_once(deadline))

    async def stop(self) -> None:
        if self._periodic_task is None:
            return
        self._stopping = True
        self._periodic_task.cancel()
        try:
            await self._periodic_task
        except asyncio.CancelledError:
            pass
        if self._worker_task is not None:
            await self._worker_task
        self._periodic_task = None
        self._worker_task = None

    async def _run_periodically(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self.start_if_eligible()
            except Exception as error:
                logger.warning(
                    "Memory consolidation scheduling failed: error_type=%s",
                    type(error).__name__,
                )

    async def _execute_once(self, deadline: float) -> None:
        try:
            await asyncio.to_thread(
                self._service.run_once,
                deadline=deadline,
                should_stop=self._should_stop,
            )
        except Exception as error:
            logger.warning(
                "Memory consolidation run failed: error_type=%s",
                type(error).__name__,
            )

    def _should_stop(self) -> bool:
        return self._stopping or not self._priority_probe()

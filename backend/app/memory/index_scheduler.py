from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

from app.memory.index_sync import MemoryIndexSync


WORKER_POLL_INTERVAL_SECONDS = 5.0
RECONCILIATION_INTERVAL_SECONDS = 3600.0
logger = logging.getLogger(__name__)


class MemoryIndexScheduler:
    def __init__(self, sync: MemoryIndexSync) -> None:
        self._sync = sync
        self.task: asyncio.Task[None] | None = None
        self._stop_requested = threading.Event()
        self._wake_event = asyncio.Event()
        self._tick_start_lock = threading.Lock()

    def start(self) -> None:
        if self.task is not None:
            raise RuntimeError("memory index scheduler is already running")
        self._stop_requested.clear()
        self._wake_event.clear()
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self.task
        if task is None:
            return
        await asyncio.to_thread(self._request_stop)
        self._wake_event.set()
        try:
            await task
        finally:
            self.task = None

    async def _run(self) -> None:
        if not await self._run_tick(self._sync.run_worker_once):
            return
        if not await self._run_tick(self._reconcile_once):
            return
        loop = asyncio.get_running_loop()
        next_reconciliation = loop.time() + RECONCILIATION_INTERVAL_SECONDS
        while not self._stop_requested.is_set():
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=WORKER_POLL_INTERVAL_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                pass
            if not await self._run_tick(self._sync.run_worker_once):
                return
            if loop.time() >= next_reconciliation:
                if not await self._run_tick(self._reconcile_once):
                    return
                next_reconciliation = loop.time() + RECONCILIATION_INTERVAL_SECONDS

    async def _run_tick(self, operation: Callable[[], None]) -> bool:
        started = await asyncio.to_thread(self._run_tick_if_active, operation)
        return started and not self._stop_requested.is_set()

    def _run_tick_if_active(self, operation: Callable[[], None]) -> bool:
        with self._tick_start_lock:
            if self._stop_requested.is_set():
                return False
        try:
            operation()
        except Exception as error:
            logger.warning(
                "memory index scheduler tick failed: %s", type(error).__name__
            )
        return True

    def _reconcile_once(self) -> None:
        self._sync.reconcile_once(should_stop=self._stop_requested.is_set)

    def _request_stop(self) -> None:
        with self._tick_start_lock:
            self._stop_requested.set()

"""会話側のsubmitは起床通知のみ。予約の正本は会話SQLiteに置く。"""

import asyncio
import logging
import math
from typing import Protocol
from collections.abc import Callable
import threading

from app.memory.formation.contracts import MemoryFormationJob
from app.memory.formation.thread_queue import ThreadFormationQueue

logger = logging.getLogger(__name__)


class DurableWorker(Protocol):
    def process_next(self, *, should_stop: Callable[[], bool]) -> bool: ...


class DurableMemoryFormationScheduler:
    def __init__(self, *, worker: DurableWorker, queue: ThreadFormationQueue, poll_seconds: float = 1) -> None:
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll interval must be positive")
        self._worker = worker
        self._queue = queue
        self._poll_seconds = poll_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = threading.Event()
        self._active = threading.Event()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("durable memory scheduler is already running")
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    def submit(self, job: MemoryFormationJob) -> None:
        del job
        if self._loop is None or self._wake is None or self._task is None or self._stop.is_set():
            raise RuntimeError("durable memory scheduler is not running")
        self._loop.call_soon_threadsafe(self._wake.set)

    def is_busy(self) -> bool:
        return self._active.is_set() or self._queue.has_pending()

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        if self._wake is not None:
            self._wake.set()
        try:
            # 実行中の登録を打ち切って半端にせず、未完了予約を残して終了する。
            await task
        finally:
            self._task = None
            self._loop = None
            self._wake = None

    async def _run(self) -> None:
        assert self._wake is not None
        while not self._stop.is_set():
            self._wake.clear()
            self._active.set()
            try:
                worked = await asyncio.to_thread(self._worker.process_next, should_stop=self._stop.is_set)
            except Exception as error:
                logger.warning("episodic formation failed: error_type=%s", type(error).__name__)
                worked = False
            finally:
                self._active.clear()
            if worked:
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

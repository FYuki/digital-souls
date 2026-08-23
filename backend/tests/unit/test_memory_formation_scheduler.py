from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from uuid import UUID

import pytest


@dataclass
class BlockingWorker:
    entered: threading.Event = field(default_factory=threading.Event)
    second_entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    calls: list[object] = field(default_factory=list)
    active: int = 0
    maximum_active: int = 0

    def process(self, job: object) -> None:
        self.calls.append(job)
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.entered.set()
        if len(self.calls) == 2:
            self.second_entered.set()
        self.release.wait(timeout=2)
        self.active -= 1


def _job(index: int):
    from app.memory.formation.contracts import MemoryFormationJob

    return MemoryFormationJob(
        character_id="miori",
        conversation_id=UUID("10000000-0000-4000-8000-000000000001"),
        turn_id=UUID(f"20000000-0000-4000-8000-{index:012d}"),
    )


def test_scheduler_submit_is_thread_safe_non_blocking_and_has_one_consumer() -> None:
    from app.memory.formation.scheduler import MemoryFormationScheduler

    async def exercise() -> BlockingWorker:
        worker = BlockingWorker()
        scheduler = MemoryFormationScheduler(
            worker=worker,
            max_queue_age_seconds=300,
            queue_maxsize=100,
        )
        await scheduler.start()

        submit_thread = threading.Thread(
            target=lambda: (scheduler.submit(_job(1)), scheduler.submit(_job(2)))
        )
        submit_thread.start()
        submit_thread.join(timeout=0.5)
        assert not submit_thread.is_alive()
        assert await asyncio.to_thread(worker.entered.wait, 1)
        assert len(worker.calls) == 1

        worker.release.set()
        assert await asyncio.to_thread(worker.second_entered.wait, 1)
        await scheduler.stop()
        return worker

    worker = asyncio.run(exercise())

    assert worker.calls == [_job(1), _job(2)]
    assert worker.maximum_active == 1


def test_scheduler_reports_busy_while_a_formation_job_is_running() -> None:
    from app.memory.formation.scheduler import MemoryFormationScheduler

    async def exercise() -> tuple[bool, bool]:
        worker = BlockingWorker()
        scheduler = MemoryFormationScheduler(
            worker=worker,
            max_queue_age_seconds=300,
            queue_maxsize=100,
        )
        await scheduler.start()
        idle = scheduler.is_busy()
        scheduler.submit(_job(1))
        assert await asyncio.to_thread(worker.entered.wait, 1)
        busy = scheduler.is_busy()
        worker.release.set()
        await scheduler.stop()
        return idle, busy

    idle, busy = asyncio.run(exercise())

    assert idle is False
    assert busy is True


def test_scheduler_clears_busy_state_after_all_queued_jobs_complete() -> None:
    from app.memory.formation.scheduler import MemoryFormationScheduler

    class RecordingWorker:
        def __init__(self) -> None:
            self.completed = threading.Event()
            self.calls = 0

        def process(self, _job: object) -> None:
            self.calls += 1
            if self.calls == 2:
                self.completed.set()

    async def exercise() -> bool:
        worker = RecordingWorker()
        scheduler = MemoryFormationScheduler(
            worker=worker,
            max_queue_age_seconds=300,
            queue_maxsize=100,
        )
        await scheduler.start()
        scheduler.submit(_job(1))
        scheduler.submit(_job(2))
        assert await asyncio.to_thread(worker.completed.wait, 1)
        for _ in range(10):
            if not scheduler.is_busy():
                break
            await asyncio.sleep(0)
        busy = scheduler.is_busy()
        await scheduler.stop()
        return busy

    assert asyncio.run(exercise()) is False


def test_expired_queue_job_is_discarded_without_worker_side_effects() -> None:
    from app.memory.formation.scheduler import MemoryFormationScheduler

    class RecordingWorker:
        def __init__(self) -> None:
            self.calls: list[object] = []
            self.called = threading.Event()

        def process(self, job: object) -> None:
            self.calls.append(job)
            self.called.set()

    async def exercise() -> RecordingWorker:
        worker = RecordingWorker()
        current_time = [0.0]
        scheduler = MemoryFormationScheduler(
            worker=worker,
            max_queue_age_seconds=300,
            queue_maxsize=100,
            monotonic_clock=lambda: current_time[0],
        )
        await scheduler.start()
        scheduler.submit(_job(1))
        current_time[0] = 301.0
        scheduler.submit(_job(2))
        assert await asyncio.to_thread(worker.called.wait, 1)
        await scheduler.stop()
        return worker

    worker = asyncio.run(exercise())

    assert worker.calls == [_job(2)]


def test_stop_discards_queued_jobs_and_waits_only_for_the_running_job() -> None:
    from app.memory.formation.scheduler import MemoryFormationScheduler

    async def exercise() -> BlockingWorker:
        worker = BlockingWorker()
        scheduler = MemoryFormationScheduler(
            worker=worker,
            max_queue_age_seconds=300,
            queue_maxsize=100,
        )
        await scheduler.start()
        scheduler.submit(_job(1))
        assert await asyncio.to_thread(worker.entered.wait, 1)
        scheduler.submit(_job(2))
        scheduler.submit(_job(3))
        await asyncio.sleep(0)
        stop_task = asyncio.create_task(scheduler.stop())
        await asyncio.sleep(0)
        worker.release.set()
        await stop_task
        return worker

    worker = asyncio.run(exercise())

    assert worker.calls == [_job(1)]


def test_worker_exception_does_not_stop_later_jobs() -> None:
    from app.memory.formation.scheduler import MemoryFormationScheduler

    class FailingOnceWorker:
        def __init__(self) -> None:
            self.calls: list[object] = []
            self.completed = threading.Event()

        def process(self, job: object) -> None:
            self.calls.append(job)
            if len(self.calls) == 1:
                raise RuntimeError("synthetic failure")
            self.completed.set()

    async def exercise() -> FailingOnceWorker:
        worker = FailingOnceWorker()
        scheduler = MemoryFormationScheduler(
            worker=worker,
            max_queue_age_seconds=300,
            queue_maxsize=100,
        )
        await scheduler.start()
        scheduler.submit(_job(1))
        scheduler.submit(_job(2))
        assert await asyncio.to_thread(worker.completed.wait, 1)
        await scheduler.stop()
        return worker

    worker = asyncio.run(exercise())

    assert worker.calls == [_job(1), _job(2)]


def test_full_queue_drops_the_newest_job_without_blocking_submit() -> None:
    from app.memory.formation.scheduler import MemoryFormationScheduler

    async def exercise() -> BlockingWorker:
        worker = BlockingWorker()
        scheduler = MemoryFormationScheduler(
            worker=worker,
            max_queue_age_seconds=300,
            queue_maxsize=1,
        )
        await scheduler.start()
        scheduler.submit(_job(1))
        assert await asyncio.to_thread(worker.entered.wait, 1)
        scheduler.submit(_job(2))
        scheduler.submit(_job(3))
        await asyncio.sleep(0)
        worker.release.set()
        assert await asyncio.to_thread(worker.second_entered.wait, 1)
        await scheduler.stop()
        return worker

    worker = asyncio.run(exercise())

    assert worker.calls == [_job(1), _job(2)]


def test_scheduler_rejects_an_unbounded_queue_configuration() -> None:
    from app.memory.formation.scheduler import MemoryFormationScheduler

    with pytest.raises(ValueError, match="queue maxsize"):
        MemoryFormationScheduler(
            worker=BlockingWorker(),
            max_queue_age_seconds=300,
            queue_maxsize=0,
        )

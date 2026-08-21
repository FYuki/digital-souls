from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from uuid import UUID


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
        scheduler = MemoryFormationScheduler(worker=worker, max_queue_age_seconds=300)
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

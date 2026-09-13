import asyncio
from uuid import uuid4

import pytest

from app.memory.formation.combined_scheduler import CombinedFormationScheduler
from app.memory.formation.contracts import MemoryFormationJob


class Scheduler:
    def __init__(self, fail_start=False, fail_stop=False, fail_submit=False):
        self.started = self.stopped = False
        self.fail_start, self.fail_stop, self.fail_submit = fail_start, fail_stop, fail_submit
        self.jobs = []
        self.busy = False

    async def start(self):
        if self.fail_start:
            raise RuntimeError("start failed")
        self.started = True

    async def stop(self):
        self.stopped = True
        if self.fail_stop:
            raise RuntimeError("stop failed")

    def submit(self, job):
        self.jobs.append(job)
        if self.fail_submit:
            raise RuntimeError("submit failed")

    def is_busy(self):
        return self.busy


def test_episode_start_failure_rolls_back_started_preference_scheduler():
    old, new = Scheduler(), Scheduler(fail_start=True)
    with pytest.raises(RuntimeError):
        asyncio.run(CombinedFormationScheduler(old, new).start())
    assert old.started and old.stopped


def test_episode_stop_failure_still_stops_preference_scheduler():
    old, new = Scheduler(), Scheduler(fail_stop=True)
    with pytest.raises(RuntimeError):
        asyncio.run(CombinedFormationScheduler(old, new).stop())
    assert old.stopped and new.stopped


def test_preference_notification_failure_does_not_lose_episode_wakeup():
    old, new = Scheduler(fail_submit=True), Scheduler()
    job = MemoryFormationJob("miori", uuid4(), uuid4())
    with pytest.raises(RuntimeError):
        CombinedFormationScheduler(old, new).submit(job)
    assert new.jobs == [job]


def test_pending_durable_work_blocks_nightly_consolidation():
    old, new = Scheduler(), Scheduler()
    new.busy = True
    assert CombinedFormationScheduler(old, new).is_busy()

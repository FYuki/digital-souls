import asyncio
import threading


class RecordingSync:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.reconciliation_completed = threading.Event()

    def run_worker_once(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append("worker")
        self.active -= 1

    def reconcile_once(self, *, should_stop=None) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append("reconcile")
        self.active -= 1
        self.reconciliation_completed.set()


def test_scheduler_runs_worker_and_reconciliation_on_one_collected_task() -> None:
    from app.memory.index_scheduler import MemoryIndexScheduler

    async def exercise() -> tuple[RecordingSync, MemoryIndexScheduler]:
        sync = RecordingSync()
        scheduler = MemoryIndexScheduler(sync)

        scheduler.start()
        reconciliation_completed = await asyncio.to_thread(
            sync.reconciliation_completed.wait,
            1,
        )
        assert reconciliation_completed
        await scheduler.stop()
        return sync, scheduler

    sync, scheduler = asyncio.run(exercise())

    assert sync.calls == ["worker", "reconcile"]
    assert sync.max_active == 1
    assert scheduler.task is None


def test_scheduler_keeps_event_loop_responsive_while_worker_is_blocked() -> None:
    from app.memory.index_scheduler import MemoryIndexScheduler

    class BlockingSync:
        def __init__(self) -> None:
            self.worker_started = threading.Event()
            self.release_worker = threading.Event()
            self.worker_observed_release = False

        def run_worker_once(self) -> None:
            self.worker_started.set()
            self.worker_observed_release = self.release_worker.wait(timeout=1)

        def reconcile_once(self, *, should_stop=None) -> None:
            return None

    async def exercise() -> BlockingSync:
        sync = BlockingSync()
        scheduler = MemoryIndexScheduler(sync)
        scheduler.start()

        async def release_from_event_loop() -> None:
            while not sync.worker_started.is_set():
                await asyncio.sleep(0.01)
            sync.release_worker.set()

        event_loop_probe = asyncio.create_task(release_from_event_loop())
        await event_loop_probe
        await scheduler.stop()
        return sync

    sync = asyncio.run(exercise())

    assert sync.worker_observed_release is True


def test_scheduler_stop_waits_for_blocking_tick() -> None:
    from app.memory.index_scheduler import MemoryIndexScheduler

    class BlockingSync:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def run_worker_once(self) -> None:
            self.started.set()
            self.release.wait(timeout=1)

        def reconcile_once(self, *, should_stop=None) -> None:
            raise AssertionError("停止通知後に新しい tick を開始してはならない")

    async def exercise() -> None:
        sync = BlockingSync()
        scheduler = MemoryIndexScheduler(sync)
        scheduler.start()
        while not sync.started.is_set():
            await asyncio.sleep(0.01)
        stopping = asyncio.create_task(scheduler.stop())
        await asyncio.sleep(0)
        event_loop_responded = False

        async def probe_event_loop() -> None:
            nonlocal event_loop_responded
            await asyncio.sleep(0)
            event_loop_responded = True

        await asyncio.wait_for(probe_event_loop(), timeout=0.1)
        assert event_loop_responded
        assert not stopping.done()
        assert scheduler.task is not None
        sync.release.set()
        await stopping
        assert scheduler.task is None

    asyncio.run(exercise())


def test_scheduler_stop_before_task_start_does_not_run_initial_tick() -> None:
    from app.memory.index_scheduler import MemoryIndexScheduler

    async def exercise() -> tuple[RecordingSync, MemoryIndexScheduler]:
        sync = RecordingSync()
        scheduler = MemoryIndexScheduler(sync)

        scheduler.start()
        await scheduler.stop()
        return sync, scheduler

    sync, scheduler = asyncio.run(exercise())

    assert sync.calls == []
    assert scheduler.task is None


def test_scheduler_stop_does_not_start_a_periodic_tick_waiting_for_a_thread(
    monkeypatch,
) -> None:
    from app.memory import index_scheduler

    async def exercise() -> RecordingSync:
        sync = RecordingSync()
        scheduler = index_scheduler.MemoryIndexScheduler(sync)
        periodic_tick_scheduled = asyncio.Event()
        release_periodic_tick = asyncio.Event()
        to_thread_calls = 0

        async def delayed_to_thread(operation, *args):
            nonlocal to_thread_calls
            to_thread_calls += 1
            if to_thread_calls == 3:
                periodic_tick_scheduled.set()
                await release_periodic_tick.wait()
            # このテストは停止後の開始順序だけを検証し、実スレッド間の競合は扱わない。
            return operation(*args)

        monkeypatch.setattr(index_scheduler.asyncio, "to_thread", delayed_to_thread)
        monkeypatch.setattr(index_scheduler, "WORKER_POLL_INTERVAL_SECONDS", 0.0)

        scheduler.start()
        await asyncio.wait_for(periodic_tick_scheduled.wait(), timeout=1)
        stopping = asyncio.create_task(scheduler.stop())
        await asyncio.sleep(0)
        release_periodic_tick.set()
        await asyncio.wait_for(stopping, timeout=1)
        return sync

    sync = asyncio.run(exercise())

    assert sync.calls == ["worker", "reconcile"]


def test_scheduler_stop_interrupts_reconciliation_at_a_cooperative_boundary() -> None:
    from app.memory.index_scheduler import MemoryIndexScheduler

    class CooperativeSync:
        def __init__(self) -> None:
            self.reconciliation_started = threading.Event()
            self.stop_observed = False

        def run_worker_once(self) -> None:
            return None

        def reconcile_once(self, *, should_stop=None) -> None:
            assert should_stop is not None
            self.reconciliation_started.set()
            while not should_stop():
                threading.Event().wait(0.01)
            self.stop_observed = True

    async def exercise() -> CooperativeSync:
        sync = CooperativeSync()
        scheduler = MemoryIndexScheduler(sync)
        scheduler.start()
        started = await asyncio.to_thread(sync.reconciliation_started.wait, 1)
        assert started
        await asyncio.wait_for(scheduler.stop(), timeout=1)
        return sync

    assert asyncio.run(exercise()).stop_observed is True


def test_scheduler_logs_tick_exception_type_and_continues(
    caplog,
) -> None:
    from app.memory.index_scheduler import MemoryIndexScheduler

    class FailingSync:
        def __init__(self) -> None:
            self.reconciliation_completed = threading.Event()

        def run_worker_once(self) -> None:
            raise RuntimeError("本文を含む秘密")

        def reconcile_once(self, *, should_stop=None) -> None:
            self.reconciliation_completed.set()

    async def exercise() -> None:
        sync = FailingSync()
        scheduler = MemoryIndexScheduler(sync)
        scheduler.start()
        completed = await asyncio.to_thread(sync.reconciliation_completed.wait, 1)
        assert completed
        await scheduler.stop()

    caplog.set_level("WARNING", logger="app.memory.index_scheduler")
    asyncio.run(exercise())

    scheduler_records = [
        record
        for record in caplog.records
        if record.name == "app.memory.index_scheduler"
    ]
    assert [record.getMessage() for record in scheduler_records] == [
        "memory index scheduler tick failed: RuntimeError"
    ]
    assert "秘密" not in "\n".join(record.getMessage() for record in scheduler_records)

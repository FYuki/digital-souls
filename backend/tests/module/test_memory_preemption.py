"""会話優先で中断した実SQLite予約を、次のworkerから重複なく回収する。"""

import asyncio
from threading import Event
from time import monotonic, sleep

import pytest

from app.inference.cancellation import raise_if_cancelled
from app.inference.errors import InferenceError
from app.memory.formation.durable_scheduler import DurableMemoryFormationScheduler
from app.memory.semantic.worker import SemanticWorkQueue, SemanticWorker
from tests.module.test_episodic_formation import Client as EpisodeClient, worker
from tests.module.test_episodic_registration import harness as harness
from tests.module.test_semantic_runtime import Client as SemanticClient, IDENTITY, pipeline
from tests.module.test_semantic_store import h as h, source


def wait_for_cancellation(started, interrupted):
    started.set()
    deadline = monotonic() + 3
    try:
        while monotonic() < deadline:
            raise_if_cancelled()
            sleep(0.005)
    except InferenceError:
        interrupted.set()
        raise
    raise AssertionError("background inference was not cancelled")


@pytest.mark.anyio
async def test_episode_reservation_survives_preemption_and_scheduler_restart(harness):
    harness.turn("うどんを食べた")
    started, interrupted = Event(), Event()
    client = EpisodeClient(callback=lambda *_: wait_for_cancellation(started, interrupted))
    available = [False]
    scheduler = DurableMemoryFormationScheduler(worker=worker(harness, client), queue=harness.queue,
                                                poll_seconds=0.01, priority_available=lambda: available[0])
    await scheduler.start()
    try:
        await asyncio.sleep(0.03)
        assert not started.is_set()
        available[0] = True
        assert await asyncio.to_thread(started.wait, 2)
        available[0] = False
        assert await asyncio.to_thread(interrupted.wait, 2)
    finally:
        await scheduler.stop()
    assert harness.records() == ()
    assert harness.queue.has_pending()
    recovered = worker(harness, EpisodeClient())
    assert recovered.process_next()
    assert len(harness.records()) == 2
    assert not harness.queue.has_pending()
    assert not recovered.process_next()
    assert len(harness.records()) == 2


@pytest.mark.anyio
async def test_semantic_preemption_does_not_advance_source_checkpoint(h):
    s = source(h)
    started, interrupted = Event(), Event()
    client = SemanticClient()
    client.callback = lambda: wait_for_cancellation(started, interrupted)
    available = [True]
    queue = SemanticWorkQueue(h.store)
    make = lambda c: SemanticWorker(queue=queue, pipeline=pipeline(h, c), identity=lambda: IDENTITY,
                                   priority_available=lambda: available[0])
    scheduler = DurableMemoryFormationScheduler(worker=make(client), queue=queue, poll_seconds=0.01,
                                                priority_available=lambda: available[0])
    await scheduler.start()
    try:
        assert await asyncio.to_thread(started.wait, 2)
        available[0] = False
        assert await asyncio.to_thread(interrupted.wait, 2)
    finally:
        await scheduler.stop()
    with h.repo.read() as tx:
        assert tx.list_records("miori") == ()
        assert not tx.processed("miori", s.conversation_id, s.source_id, s.revision)
    assert queue.has_pending()
    available[0] = True
    recovered = make(SemanticClient())
    assert recovered.process_next(should_stop=lambda: False)
    assert not queue.has_pending()
    assert not recovered.process_next(should_stop=lambda: False)
    with h.repo.read() as tx:
        assert len(tx.list_records("miori")) == 1


def test_episode_unrelated_error_is_not_hidden_by_simultaneous_stop(harness):
    harness.turn("うどんを食べた")
    stopped = [False]
    def fail(*_):
        stopped[0] = True
        raise RuntimeError("synthetic unrelated failure")
    with pytest.raises(RuntimeError, match="synthetic unrelated"):
        worker(harness, EpisodeClient(callback=fail)).process_next(should_stop=lambda: stopped[0])
    assert harness.queue.has_pending()
    assert harness.records() == ()


def test_semantic_unrelated_error_keeps_backoff_during_simultaneous_stop(h):
    source(h)
    stopped = [False]
    client = SemanticClient()
    def fail():
        stopped[0] = True
        raise RuntimeError("synthetic unrelated failure")
    client.callback = fail
    queue = SemanticWorkQueue(h.store)
    instance = SemanticWorker(queue=queue, pipeline=pipeline(h, client), identity=lambda: IDENTITY,
                              priority_available=lambda: True)
    with pytest.raises(RuntimeError, match="synthetic unrelated"):
        instance.process_next(should_stop=lambda: stopped[0])
    assert instance.backoff
    assert queue.has_pending()

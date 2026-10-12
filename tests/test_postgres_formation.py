"""Synthetic real-PostgreSQL contracts for durable, finite formation scheduling."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any

import pytest
from psycopg import Connection

from digital_souls_core.api import create_app
from digital_souls_core.application import CoreError, Inference
from digital_souls_core.character import AccessScope
from digital_souls_core.conversations import Conversations
from digital_souls_core.formation import DrainResult, FormationConfig, FormationInput
from digital_souls_core.history import (
    Binding,
    ConversationControls,
    MemoryConfirmation,
    TurnDeletionInput,
)
from digital_souls_core.memory_record_store import RecordBatch
from digital_souls_core.memory_records import RecordKind
from digital_souls_core.postgres_db import PostgresDatabase
from digital_souls_core.postgres_formation import PostgresFormation
from digital_souls_core.postgres_history import PostgresHistory
from digital_souls_core.privacy import PrivacyPolicy

from . import test_postgres_stores
from .conversation_support import turn
from .postgres_record_support import source_episode
from .privacy_support import local_profile
from .support import FakeProvider, character
from .test_postgres_stores import BINDING, MESSAGES, Stores

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC) + timedelta(seconds=1)

    def __call__(self) -> datetime:
        return self.now


class Processor:
    def __init__(self) -> None:
        self.calls: list[FormationInput] = []
        self.fail = False

    async def process(self, job: FormationInput) -> None:
        self.calls.append(job)
        if self.fail:
            raise RuntimeError("Synthetic processor text must never escape")


def enabled(
    stores: Stores, *, config: FormationConfig | None = None
) -> tuple[PostgresFormation, Processor, Clock]:
    policy = PrivacyPolicy()
    policy.configure({BINDING: frozenset({"history", "local", "memory"})})
    history = PostgresHistory(stores.database, formation_policy=policy)
    processor = Processor()
    clock = Clock()
    return (
        PostgresFormation(history, policy, processor, config=config, clock=clock),
        processor,
        clock,
    )


def append(
    service: PostgresFormation,
    cid: str,
    revision: int,
    binding: Binding = BINDING,
    /,
    **kwargs: Any,
) -> None:
    service.history.append(
        binding, cid, f"r{revision + 1}", f"fp{revision + 1}", revision, MESSAGES, "stop", **kwargs
    )


def ledger(stores: Stores) -> list[tuple[Any, ...]]:
    with stores.database.transaction(BINDING) as db:
        return db.execute(
            "SELECT target_revision,state,attempts,leased_revision,lease_token,"
            "lease_until,last_code FROM formation_jobs ORDER BY seq"
        ).fetchall()


async def test_reservation_coalesces_and_duplicate_receipt_does_not_rereserve(
    stores: Stores,
) -> None:
    service, processor, _ = enabled(stores)
    cid = service.history.create(BINDING).conversation_id
    assert ledger(stores) == []
    append(service, cid, 0)
    append(service, cid, 1)
    assert len(ledger(stores)) == 1 and ledger(stores)[0][:3] == (2, "pending", 0)
    result = await service.drain()
    assert result == DrainResult(succeeded=1) and result.status == "success"
    assert len(processor.calls) == 1 and processor.calls[0].origin_revision == 2
    assert len(processor.calls[0].origin_sources) == 4
    append(service, cid, 1)
    assert ledger(stores)[0][1] == "done"
    assert await service.drain() == DrainResult()
    assert "I like synthetic tea" not in str(ledger(stores))


async def test_update_while_leased_rereserves_and_drain_does_not_loop_same_thread(
    stores: Stores,
) -> None:
    service, processor, _ = enabled(stores)
    cid = service.history.create(BINDING).conversation_id
    append(service, cid, 0)

    class Updating:
        async def process(self, job: FormationInput) -> None:
            assert ledger(stores)[0][:4] == (1, "leased", 1, 1)
            append(service, cid, 1)
            assert ledger(stores)[0][:4] == (2, "leased", 1, 1)

    service.processor = Updating()
    assert await service.drain() == DrainResult(succeeded=1)
    assert ledger(stores)[0][:4] == (2, "pending", 0, None)
    service.processor = processor
    assert await service.drain() == DrainResult(succeeded=1)
    assert processor.calls[0].origin_revision == 2


async def test_failed_old_revision_does_not_exhaust_updated_input(stores: Stores) -> None:
    service, processor, _ = enabled(stores, config=FormationConfig(max_attempts=1))
    cid = service.history.create(BINDING).conversation_id
    append(service, cid, 0)

    class UpdatingFailure:
        async def process(self, job: FormationInput) -> None:
            append(service, cid, 1)
            raise ValueError("Synthetic failure")

    service.processor = UpdatingFailure()
    assert (await service.drain()).failed == 1
    assert ledger(stores)[0][:3] == (2, "pending", 0)
    service.processor = processor
    assert (await service.drain()).succeeded == 1


async def test_fail_backoff_and_restart_retry_then_retain_exhausted_job(stores: Stores) -> None:
    config = FormationConfig(max_attempts=2, retry_backoff_seconds=5)
    service, processor, clock = enabled(stores, config=config)
    cid = service.history.create(BINDING).conversation_id
    append(service, cid, 0)
    processor.fail = True
    result = await service.drain()
    assert result == DrainResult(failed=1) and result.status == "failure"
    assert ledger(stores)[0][:3] == (1, "pending", 1)
    assert "Synthetic processor" not in str(ledger(stores))
    assert await service.drain() == DrainResult()
    clock.now += timedelta(seconds=5)
    history = PostgresHistory(PostgresDatabase(stores.config), formation_policy=service.policy)
    restarted = PostgresFormation(history, service.policy, processor, config=config, clock=clock)
    assert await restarted.drain() == DrainResult(failed=1)
    assert ledger(stores)[0][:3] == (1, "failed", 2)
    clock.now += timedelta(seconds=100)
    assert await restarted.drain() == DrainResult()
    processor.fail = False
    append(restarted, cid, 1)
    assert await restarted.drain() == DrainResult(succeeded=1)


async def test_interrupt_after_canonical_write_restarts_idempotently(stores: Stores) -> None:
    service, _, clock = enabled(stores)
    cid = service.history.create(BINDING).conversation_id
    append(service, cid, 0)
    calls: list[FormationInput] = []

    class Canonical:
        async def process(self, job: FormationInput) -> None:
            calls.append(job)
            refs = tuple(s.state.reference for s in job.origin_sources if s.message.role == "user")
            batch = RecordBatch(episodes=(source_episode(service.history, refs),))
            stores.records.register(job.origin_binding, batch, "synthetic-formation-v1")
            if len(calls) == 1:
                raise asyncio.CancelledError

    processor = Canonical()
    service.processor = processor
    with pytest.raises(asyncio.CancelledError):
        await service.drain()
    lease = ledger(stores)[0]
    assert lease[1] == "leased" and lease[4] is not None
    assert len(stores.records.list(BINDING, RecordKind.EPISODE)) == 1
    history = PostgresHistory(PostgresDatabase(stores.config), formation_policy=service.policy)
    restarted = PostgresFormation(history, service.policy, processor, clock=clock)
    assert await restarted.drain() == DrainResult()
    clock.now += timedelta(seconds=service.config.lease_seconds)
    assert await restarted.drain() == DrainResult(succeeded=1)
    assert calls[0] == calls[1]
    assert len(stores.records.list(BINDING, RecordKind.EPISODE)) == 1
    assert ledger(stores)[0][:3] == (1, "done", 2)
    with stores.database.transaction(BINDING) as db:
        assert db.execute("SELECT count(*) FROM memory_record_registrations").fetchone() == (1,)


async def test_live_processor_never_overlaps_even_after_lease_expiry(stores: Stores) -> None:
    service, processor, clock = enabled(stores, config=FormationConfig(lease_seconds=1))
    cid = service.history.create(BINDING).conversation_id
    append(service, cid, 0)
    started, release = asyncio.Event(), asyncio.Event()

    class Paused:
        async def process(self, job: FormationInput) -> None:
            processor.calls.append(job)
            started.set()
            await release.wait()

    service.processor = Paused()
    active = asyncio.create_task(service.drain())
    try:
        await asyncio.wait_for(started.wait(), 5)
        clock.now += timedelta(seconds=2)
        other = PostgresFormation(
            PostgresHistory(PostgresDatabase(stores.config), formation_policy=service.policy),
            service.policy,
            processor,
            clock=clock,
        )
        assert await other.drain() == DrainResult()
        assert len(processor.calls) == 1
        append(service, cid, 1)  # Conversation commit is independent of active processor.
    finally:
        release.set()
        await asyncio.wait_for(active, 5)
    assert ledger(stores)[0][:3] == (2, "pending", 0)


def test_concurrent_connections_claim_each_job_only_once(stores: Stores) -> None:
    service, processor, clock = enabled(stores)
    for _ in range(4):
        append(service, service.history.create(BINDING).conversation_id, 0)
    barrier = Barrier(2)

    def run() -> DrainResult:
        other = PostgresFormation(
            PostgresHistory(PostgresDatabase(stores.config), formation_policy=service.policy),
            service.policy,
            processor,
            clock=clock,
        )
        barrier.wait(timeout=5)
        return asyncio.run(other.drain())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sum(result.succeeded for result in results) == 4
    assert len({job.origin_conversation_id for job in processor.calls}) == 4
    assert len(processor.calls) == 4
    assert all(row[1] == "done" for row in ledger(stores))


async def test_finite_job_limit_keeps_backlog_and_requires_processor(stores: Stores) -> None:
    service, processor, _ = enabled(stores, config=FormationConfig(max_jobs=2))
    for _ in range(3):
        append(service, service.history.create(BINDING).conversation_id, 0)
    service.processor = None
    with pytest.raises(CoreError) as error:
        await service.drain()
    assert error.value.code == "formation_processor_unavailable"
    assert all(row[1:3] == ("pending", 0) for row in ledger(stores))
    service.processor = processor
    assert await service.drain(max_jobs=1) == DrainResult(succeeded=1)
    assert sum(row[1] == "pending" for row in ledger(stores)) == 2
    assert await service.drain() == DrainResult(succeeded=2)
    with pytest.raises(CoreError):
        await service.drain(max_jobs=3)


@pytest.mark.parametrize(
    "mode",
    ["private", "conversation_delete", "turn_delete", "excluded", "held", "permission_revoked"],
)
async def test_ineligible_source_never_reaches_processor(stores: Stores, mode: str) -> None:
    service, processor, _ = enabled(stores)
    cid = service.history.create(BINDING).conversation_id
    append(
        service,
        cid,
        0,
        **(
            {"memory_excluded_indices": (0,)}
            if mode == "excluded"
            else {"memory_confirmation_indices": (0,)}
            if mode == "held"
            else {}
        ),
    )
    if mode == "private":
        service.history.controls(
            BINDING, cid, ConversationControls(expected_revision=1, private_mode=True)
        )
    elif mode == "conversation_delete":
        service.history.delete(BINDING, cid)
        assert ledger(stores) == []
    elif mode == "turn_delete":
        service.history.delete_turns(
            BINDING, cid, TurnDeletionInput(expected_revision=1, turn_revision=1, scope="selected")
        )
    elif mode == "permission_revoked":
        service.policy.configure({})
    result = await service.drain()
    assert result == DrainResult(excluded=0 if mode == "conversation_delete" else 1)
    assert processor.calls == []


async def test_partial_deletion_preserves_later_source_and_confirmation_releases_reservation(
    stores: Stores,
) -> None:
    service, processor, _ = enabled(stores)
    cid = service.history.create(BINDING).conversation_id
    append(service, cid, 0, memory_confirmation_indices=(0,))
    append(service, cid, 1)
    service.history.delete_turns(
        BINDING, cid, TurnDeletionInput(expected_revision=2, turn_revision=1, scope="selected")
    )
    assert (await service.drain()).succeeded == 1
    assert [
        (s.state.reference.turn_revision, s.message.role) for s in processor.calls[0].origin_sources
    ] == [(2, "user")]
    other = service.history.create(BINDING).conversation_id
    append(service, other, 0, memory_confirmation_indices=(0,))
    assert (await service.drain()).excluded == 1
    service.history.confirm(
        BINDING,
        other,
        MemoryConfirmation(
            expected_revision=1, turn_revision=1, message_index=0, accept_private_mode=False
        ),
    )
    assert (await service.drain()).succeeded == 1
    assert processor.calls[-1].origin_conversation_id == other
    assert processor.calls[-1].origin_sources[0].state.reference.turn_revision == 1


@pytest.mark.parametrize("enabled_formation,memory", [(False, True), (True, False), (False, False)])
def test_disabled_or_memory_permission_missing_does_not_reserve(
    stores: Stores, enabled_formation: bool, memory: bool
) -> None:
    policy = PrivacyPolicy()
    policy.configure({BINDING: frozenset({"memory"}) if memory else frozenset({"history"})})
    history = PostgresHistory(
        stores.database, formation_policy=policy if enabled_formation else None
    )
    cid = history.create(BINDING).conversation_id
    history.append(BINDING, cid, "r1", "fp1", 0, MESSAGES, "stop")
    history.controls(BINDING, cid, ConversationControls(expected_revision=1, archived=True))
    assert ledger(stores) == []


async def test_binding_isolation_and_archive_does_not_exclude(stores: Stores) -> None:
    service, processor, _ = enabled(stores)
    other = Binding(AccessScope(subject="synthetic-other"), "synthetic")
    cid = service.history.create(other).conversation_id
    append(service, cid, 0, other)
    assert ledger(stores) == []
    service.policy.configure({BINDING: frozenset({"memory"}), other: frozenset({"memory"})})
    append(service, cid, 1, other)
    service.history.controls(other, cid, ConversationControls(expected_revision=2, archived=True))
    assert (await service.drain()).succeeded == 1
    assert processor.calls[0].origin_binding == other
    assert processor.calls[0].origin_revision == 3
    assert service.history.list(BINDING) == []


def test_reservation_failure_rolls_back_history(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _ = enabled(stores)
    cid = service.history.create(BINDING).conversation_id
    execute = Connection.execute

    def fail(db: Connection[tuple[Any, ...]], query: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(db, query, *args, **kwargs)
        if str(query).startswith("INSERT INTO formation_jobs"):
            raise RuntimeError("Synthetic reservation interruption")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Connection, "execute", fail)
        with pytest.raises(RuntimeError, match="Synthetic reservation"):
            append(service, cid, 0)
    assert service.history.read(BINDING, cid).revision == 0
    assert service.history.receipt(BINDING, cid, "r1") is None
    assert ledger(stores) == []
    append(service, cid, 0)
    assert ledger(stores)[0][:2] == (1, "pending")


async def test_conversation_response_does_not_call_or_wait_for_processor(stores: Stores) -> None:
    service, processor, _ = enabled(stores)
    char = character("synthetic")
    char = replace(char, config=char.config.model_copy(update={"profile": local_profile()}))
    provider = FakeProvider()
    inference = Inference((char,), provider, privacy=service.policy)
    conversations = Conversations(inference, service.history, service.policy)
    cid = conversations.create("synthetic").conversation_id
    receipt = await conversations.complete("synthetic", cid, turn())
    assert receipt.revision == 1 and len(provider.calls) == 1
    assert processor.calls == []
    assert ledger(stores)[0][:2] == (1, "pending")
    app = create_app(
        inference, history_store=service.history, history_policy=service.policy, formation=service
    )
    assert any(getattr(route, "path", "") == "/v1/memory/formation/drain" for route in app.routes)


async def test_updated_input_after_interruption_gets_fresh_attempt_budget(stores: Stores) -> None:
    service, processor, clock = enabled(
        stores, config=FormationConfig(max_attempts=1, lease_seconds=1)
    )
    cid = service.history.create(BINDING).conversation_id
    append(service, cid, 0)

    class Interrupted:
        async def process(self, job: FormationInput) -> None:
            append(service, cid, 1)
            raise asyncio.CancelledError

    service.processor = Interrupted()
    with pytest.raises(asyncio.CancelledError):
        await service.drain()
    service.processor = processor
    clock.now += timedelta(seconds=2)
    assert await service.drain() == DrainResult(succeeded=1)
    assert processor.calls[0].origin_revision == 2
    assert ledger(stores)[0][:3] == (2, "done", 1)


async def test_timeout_is_failure_and_keeps_pending_for_retry(stores: Stores) -> None:
    service, _, clock = enabled(
        stores, config=FormationConfig(processor_timeout_seconds=1, retry_backoff_seconds=0)
    )
    cid = service.history.create(BINDING).conversation_id
    append(service, cid, 0)
    cancelled = False

    class Timeout:
        async def process(self, job: FormationInput) -> None:
            nonlocal cancelled
            try:
                await asyncio.Event().wait()
            finally:
                cancelled = True

    service.processor = Timeout()
    assert await service.drain() == DrainResult(failed=1)
    assert cancelled
    assert ledger(stores)[0][:3] == (1, "pending", 1)
    assert ledger(stores)[0][-1] == "processor_timeout"
    clock.now += timedelta(seconds=1)
    assert await service.drain(max_jobs=1) == DrainResult(failed=1)


async def test_processor_chooses_reads_independently_of_reservation_origin(stores: Stores) -> None:
    service, _, _ = enabled(stores)
    other_binding = Binding(AccessScope(subject="synthetic-other"), "synthetic")
    service.policy.configure({BINDING: frozenset({"memory"}), other_binding: frozenset({"memory"})})
    # Existing synthetic history is not automatically reserved or imported.
    other_cid = stores.history.create(other_binding).conversation_id
    stores.history.append(other_binding, other_cid, "other-r1", "fp-other", 0, MESSAGES, "stop")
    origin_cid = service.history.create(BINDING).conversation_id
    append(service, origin_cid, 0)
    reads: list[str] = []

    class ScopeChoosing:
        async def process(self, job: FormationInput) -> None:
            assert job.origin_binding == BINDING
            assert job.origin_conversation_id == origin_cid
            assert service.policy.permits(other_binding, "memory")
            chosen = stores.history.read(other_binding, other_cid)
            reads.append(chosen.conversation_id)
            assert chosen.messages == MESSAGES

    service.processor = ScopeChoosing()
    assert await service.drain() == DrainResult(succeeded=1)
    assert reads == [other_cid]
    assert len(ledger(stores)) == 1


async def test_reduced_retry_budget_preserves_recorded_attempts(stores: Stores) -> None:
    service, processor, clock = enabled(stores, config=FormationConfig(retry_backoff_seconds=0))
    cid = service.history.create(BINDING).conversation_id
    append(service, cid, 0)
    processor.fail = True
    assert (await service.drain()).failed == 1
    assert (await service.drain()).failed == 1
    assert ledger(stores)[0][:3] == (1, "pending", 2)
    reopened = PostgresFormation(
        service.history,
        service.policy,
        processor,
        config=FormationConfig(max_attempts=1),
        clock=clock,
    )
    assert (await reopened.drain()).failed == 1
    assert ledger(stores)[0][:3] == (1, "failed", 2)
    assert len(processor.calls) == 2

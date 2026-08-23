from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.memory.persistence.contracts import ApprovedMemory, ApprovedMemoryDetail
from tests.unit._helpers import approved_memory


PRIVATE_MARKERS = (
    "synthetic-candidate-body",
    "synthetic-prompt-body",
    "synthetic-raw-model-output",
    "synthetic-exception-body",
)


class FailingClient:
    def chat(self, *_args: object, **_kwargs: object) -> str:
        raise RuntimeError(PRIVATE_MARKERS[3])


class AmbiguousClient:
    def chat(self, *_args: object, **_kwargs: object) -> str:
        return PRIVATE_MARKERS[2]


class UntrustedReasonClient:
    def chat(self, *_args: object, **_kwargs: object) -> str:
        return json.dumps(
            {
                "plans": [
                    {
                        "plan_type": "KEEP",
                        "reason_code": "SYNTHETIC_CANDIDATE_BODY",
                        "memories": [],
                    }
                ]
            }
        )


class RecordingTimeoutClient:
    def __init__(self, memory: ApprovedMemory) -> None:
        self._memory = memory
        self.timeouts: list[float] = []

    def chat(self, *_args: object, **kwargs: object) -> str:
        timeout_seconds = kwargs["timeout_seconds"]
        assert isinstance(timeout_seconds, float)
        self.timeouts.append(timeout_seconds)
        return json.dumps(
            {
                "plans": [
                    {
                        "plan_type": "KEEP",
                        "reason_code": "MODEL_SELECTED",
                        "memories": [
                            {
                                "memory_id": str(self._memory.id),
                                "content_version": self._memory.content_version,
                            }
                        ],
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    ("client", "reason_code"),
    (
        (FailingClient(), "MODEL_FAILURE"),
        (AmbiguousClient(), "AMBIGUOUS"),
        (UntrustedReasonClient(), "AMBIGUOUS"),
    ),
)
def test_planner_normalizes_model_failure_and_ambiguous_output_to_safe_noop(
    client: object,
    reason_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.memory.consolidation.contracts import ConsolidationPlanType
    from app.memory.consolidation.planner import ConsolidationPlanner

    candidate = approved_memory(normalized_text=PRIVATE_MARKERS[0])
    planner = ConsolidationPlanner(
        client=client,
        max_output_tokens=512,
        model_id="gemma4:e4b",
        prompt_version="consolidation-v1",
        policy_version="policy-v1",
    )
    caplog.set_level(logging.DEBUG)

    response = planner.plan((candidate,), timeout_seconds=15)

    assert len(response.plans) == 1
    assert response.plans[0].plan_type is ConsolidationPlanType.NOOP
    assert response.plans[0].reason_code == reason_code
    assert [ref.memory_id for ref in response.plans[0].memories] == [candidate.id]
    observed = "\n".join(
        f"{record.getMessage()} {record.args!r} {record.exc_text or ''}"
        for record in caplog.records
    )
    assert reason_code in observed
    for marker in PRIVATE_MARKERS:
        assert marker not in observed
    assert "SYNTHETIC_CANDIDATE_BODY" not in observed


def test_planner_passes_the_resolved_deadline_timeout_to_the_client() -> None:
    from app.memory.consolidation.planner import ConsolidationPlanner

    candidate = approved_memory()
    client = RecordingTimeoutClient(candidate)
    planner = ConsolidationPlanner(
        client=client,
        max_output_tokens=512,
        model_id="gemma4:e4b",
        prompt_version="consolidation-v1",
        policy_version="policy-v1",
    )

    planner.plan((candidate,), timeout_seconds=0.25)

    assert client.timeouts == [0.25]


def test_runtime_settings_resolve_all_consolidation_limits_at_the_boundary() -> None:
    from app.memory.consolidation.config import resolve_memory_consolidation_settings

    settings = resolve_memory_consolidation_settings(
        {
            "MEMORY_CONSOLIDATION_INTERVAL_SECONDS": "601",
            "MEMORY_CONSOLIDATION_IDLE_SECONDS": "901",
            "MEMORY_CONSOLIDATION_BATCH_SIZE": "17",
            "MEMORY_CONSOLIDATION_MAX_RUNTIME_SECONDS": "121",
        }
    )

    assert settings.interval_seconds == 601
    assert settings.idle_seconds == 901
    assert settings.batch_size == 17
    assert settings.max_runtime_seconds == 121


@pytest.mark.parametrize(
    "key",
    (
        "MEMORY_CONSOLIDATION_INTERVAL_SECONDS",
        "MEMORY_CONSOLIDATION_IDLE_SECONDS",
        "MEMORY_CONSOLIDATION_BATCH_SIZE",
        "MEMORY_CONSOLIDATION_MAX_RUNTIME_SECONDS",
    ),
)
def test_runtime_settings_reject_non_positive_or_ambiguous_values(key: str) -> None:
    from app.memory.consolidation.config import resolve_memory_consolidation_settings

    for value in ("0", "-1", " 2", "2.5", ""):
        with pytest.raises(ValueError):
            resolve_memory_consolidation_settings({key: value})


def test_eligibility_requires_night_or_idle_and_yields_to_foreground_work() -> None:
    from app.memory.consolidation.scheduler import ConsolidationPriorityState
    from app.memory.consolidation.scheduler import is_consolidation_eligible

    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    idle = ConsolidationPriorityState(
        latest_conversation_activity_at=now - timedelta(minutes=31),
        processing_conversation_count=0,
        formation_busy=False,
        pending_outbox_count=0,
    )
    recent = ConsolidationPriorityState(
        latest_conversation_activity_at=now - timedelta(minutes=1),
        processing_conversation_count=0,
        formation_busy=False,
        pending_outbox_count=0,
    )

    assert (
        is_consolidation_eligible(
            now=now,
            priority=idle,
            idle_seconds=1800,
            nightly_start_hour=0,
            nightly_end_hour=6,
        )
        is True
    )
    assert (
        is_consolidation_eligible(
            now=now,
            priority=recent,
            idle_seconds=1800,
            nightly_start_hour=0,
            nightly_end_hour=6,
        )
        is False
    )
    for busy in (
        replace_priority(idle, processing_conversation_count=1),
        replace_priority(idle, formation_busy=True),
        replace_priority(idle, pending_outbox_count=1),
    ):
        assert (
            is_consolidation_eligible(
                now=now,
                priority=busy,
                idle_seconds=1800,
                nightly_start_hour=0,
                nightly_end_hour=6,
            )
            is False
        )


def test_priority_probe_reads_metadata_from_the_owning_runtime_components() -> None:
    from app.memory.consolidation.scheduler import ConsolidationPriorityProbe

    latest_activity = datetime(2026, 8, 23, 1, 2, tzinfo=UTC)
    conversation_repository = Mock()
    conversation_repository.consolidation_activity.return_value = (
        2,
        latest_activity,
    )
    formation_scheduler = Mock()
    formation_scheduler.is_busy.return_value = True
    outbox_repository = Mock()
    outbox_repository.status_counts.return_value = (3, 4)

    state = ConsolidationPriorityProbe(
        conversation_repository=conversation_repository,
        formation_scheduler=formation_scheduler,
        outbox_repository=outbox_repository,
    ).read()

    assert state.latest_conversation_activity_at == latest_activity
    assert state.processing_conversation_count == 2
    assert state.formation_busy is True
    assert state.pending_outbox_count == 7
    conversation_repository.consolidation_activity.assert_called_once_with()
    formation_scheduler.is_busy.assert_called_once_with()
    outbox_repository.status_counts.assert_called_once_with()


def replace_priority(priority, **changes: object):
    from dataclasses import replace

    return replace(priority, **changes)


@dataclass
class BlockingService:
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    calls: int = 0
    active: int = 0
    maximum_active: int = 0

    def run_once(self, *, deadline: float, should_stop) -> None:
        assert deadline > 0
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.entered.set()
        self.release.wait(timeout=2)
        self.active -= 1


def test_scheduler_cleans_up_periodic_task_when_initial_probe_fails() -> None:
    from app.memory.consolidation.scheduler import MemoryConsolidationScheduler

    async def exercise() -> None:
        probe_fails = True

        def priority_probe() -> bool:
            if probe_fails:
                raise RuntimeError("synthetic probe failure")
            return False

        scheduler = MemoryConsolidationScheduler(
            service=Mock(),
            interval_seconds=60,
            max_runtime_seconds=60,
            priority_probe=priority_probe,
        )
        with pytest.raises(RuntimeError, match="synthetic probe failure"):
            await scheduler.start()
        assert scheduler._periodic_task is None

        probe_fails = False
        await scheduler.start()
        assert scheduler._periodic_task is not None
        await scheduler.stop()

    asyncio.run(exercise())


def test_scheduler_uses_one_worker_and_stops_cooperatively() -> None:
    from app.memory.consolidation.scheduler import MemoryConsolidationScheduler

    async def exercise() -> BlockingService:
        service = BlockingService()
        scheduler = MemoryConsolidationScheduler(
            service=service,
            interval_seconds=1,
            max_runtime_seconds=60,
            priority_probe=lambda: True,
        )
        await scheduler.start()
        assert await asyncio.to_thread(service.entered.wait, 1)
        await scheduler.start_if_eligible()
        await scheduler.start_if_eligible()
        stop_task = asyncio.create_task(scheduler.stop())
        await asyncio.sleep(0)
        service.release.set()
        await stop_task
        return service

    service = asyncio.run(exercise())

    assert service.maximum_active == 1
    assert service.calls == 1


@dataclass
class YieldingService:
    priority_available: threading.Event
    completed: threading.Event = field(default_factory=threading.Event)
    processed_units: list[str] = field(default_factory=list)

    def run_once(self, *, deadline: float, should_stop) -> None:
        assert deadline > 0
        for memory_id in ("memory-1", "memory-2"):
            if should_stop():
                break
            self.processed_units.append(memory_id)
            self.priority_available.clear()
        self.completed.set()


def test_scheduler_yields_before_the_next_memory_when_priority_changes() -> None:
    from app.memory.consolidation.scheduler import MemoryConsolidationScheduler

    async def exercise() -> YieldingService:
        priority_available = threading.Event()
        priority_available.set()
        service = YieldingService(priority_available)
        scheduler = MemoryConsolidationScheduler(
            service=service,
            interval_seconds=60,
            max_runtime_seconds=60,
            priority_probe=priority_available.is_set,
        )
        await scheduler.start()
        assert await asyncio.to_thread(service.completed.wait, 1)
        await scheduler.stop()
        return service

    service = asyncio.run(exercise())

    assert service.processed_units == ["memory-1"]


@dataclass
class MemoryUnitResumingService:
    remaining: list[str] = field(default_factory=lambda: ["memory-1", "memory-2"])
    completed: list[str] = field(default_factory=list)
    calls: int = 0
    call_finished: threading.Event = field(default_factory=threading.Event)

    def run_once(self, *, deadline: float, should_stop) -> None:
        assert deadline > 0
        assert should_stop() is False
        self.calls += 1
        self.completed.append(self.remaining.pop(0))
        self.call_finished.set()


def test_scheduler_allows_the_next_run_to_resume_at_the_next_memory_unit() -> None:
    from app.memory.consolidation.scheduler import MemoryConsolidationScheduler

    async def exercise() -> MemoryUnitResumingService:
        service = MemoryUnitResumingService()
        scheduler = MemoryConsolidationScheduler(
            service=service,
            interval_seconds=60,
            max_runtime_seconds=1,
            priority_probe=lambda: True,
        )
        await scheduler.start()
        assert await asyncio.to_thread(service.call_finished.wait, 1)
        service.call_finished.clear()
        await scheduler.start_if_eligible()
        assert await asyncio.to_thread(service.call_finished.wait, 1)
        await scheduler.stop()
        return service

    service = asyncio.run(exercise())

    assert service.completed == ["memory-1", "memory-2"]
    assert service.remaining == []
    assert service.calls == 2


@dataclass
class ControlledMonotonicClock:
    now: float

    def __call__(self) -> float:
        return self.now


@dataclass
class DeadlineRepository:
    remaining: dict[UUID, ApprovedMemoryDetail]
    monotonic_clock: ControlledMonotonicClock
    applied: list[UUID] = field(default_factory=list)

    def list_character_ids(self) -> set[str]:
        return {"miori"}

    def list_by_provider(self, **_kwargs: object) -> list[ApprovedMemory]:
        return [detail.memory for detail in self.remaining.values()]

    def get_details(self, **kwargs: object) -> dict[UUID, ApprovedMemoryDetail]:
        memory_ids = kwargs["memory_ids"]
        assert isinstance(memory_ids, tuple)
        return {
            memory_id: self.remaining[memory_id]
            for memory_id in memory_ids
            if memory_id in self.remaining
        }

    def apply_consolidation(self, **kwargs: object) -> ApprovedMemory:
        canonical_memory_id = kwargs["canonical_memory_id"]
        assert isinstance(canonical_memory_id, UUID)
        detail = self.remaining.pop(canonical_memory_id)
        self.applied.append(canonical_memory_id)
        self.monotonic_clock.now = 10.0
        return detail.memory


@dataclass
class KeepPlanner:
    planned: list[tuple[UUID, ...]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def plan(self, memories: tuple[ApprovedMemory, ...], *, timeout_seconds: float):
        from app.memory.consolidation.contracts import MemoryVersionRef
        from app.memory.consolidation.planner import parse_consolidation_response

        memory_ids = tuple(memory.id for memory in memories)
        self.planned.append(memory_ids)
        self.timeouts.append(timeout_seconds)
        refs = tuple(
            MemoryVersionRef(
                memory_id=memory.id,
                content_version=memory.content_version,
            )
            for memory in memories
        )
        return parse_consolidation_response(
            json.dumps(
                {
                    "plans": [
                        {
                            "plan_type": "KEEP",
                            "reason_code": "MODEL_SELECTED",
                            "memories": [
                                {
                                    "memory_id": str(memory.id),
                                    "content_version": memory.content_version,
                                }
                                for memory in memories
                            ],
                        }
                    ]
                }
            ),
            expected_memories=refs,
        )


def test_service_stops_at_deadline_before_starting_the_next_memory_unit() -> None:
    from app.memory.consolidation.service import MemoryConsolidationService

    details = {
        memory_id: _detail_for_runtime(memory_id)
        for memory_id in (
            UUID("00000000-0000-4000-8000-000000000001"),
            UUID("00000000-0000-4000-8000-000000000002"),
        )
    }
    monotonic_clock = ControlledMonotonicClock(now=9.0)
    repository = DeadlineRepository(details, monotonic_clock)
    planner = KeepPlanner()
    service = MemoryConsolidationService(
        repository=repository,
        planner=planner,
        privacy_reviewer=Mock(),
        batch_size=1,
        llm_timeout_seconds=15,
        clock=lambda: datetime(2026, 8, 23, tzinfo=UTC),
        monotonic_clock=monotonic_clock,
        model_id="gemma4:e4b",
        prompt_version="consolidation-v1",
        policy_version="policy-v1",
    )

    service.run_once(deadline=10.0, should_stop=lambda: False)

    assert repository.applied == [UUID("00000000-0000-4000-8000-000000000001")]
    assert set(repository.remaining) == {UUID("00000000-0000-4000-8000-000000000002")}

    monotonic_clock.now = 9.0
    service.run_once(deadline=10.0, should_stop=lambda: False)

    assert repository.applied == [
        UUID("00000000-0000-4000-8000-000000000001"),
        UUID("00000000-0000-4000-8000-000000000002"),
    ]
    assert repository.remaining == {}
    assert planner.planned == [
        (UUID("00000000-0000-4000-8000-000000000001"),),
        (UUID("00000000-0000-4000-8000-000000000002"),),
    ]
    assert planner.timeouts == [1.0, 1.0]


def test_service_excludes_memories_consolidated_within_the_reprocess_interval() -> None:
    from app.memory.consolidation.service import MemoryConsolidationService

    now = datetime(2026, 8, 23, tzinfo=UTC)
    repository = Mock()
    repository.list_character_ids.return_value = {"miori"}
    repository.list_by_provider.return_value = [
        approved_memory(last_consolidated_at=now - timedelta(minutes=30))
    ]
    planner = Mock()
    service = MemoryConsolidationService(
        repository=repository,
        planner=planner,
        privacy_reviewer=Mock(),
        batch_size=10,
        llm_timeout_seconds=15,
        clock=lambda: now,
        model_id="gemma4:e4b",
        prompt_version="consolidation-v1",
        policy_version="policy-v1",
        reprocess_interval_seconds=3600,
    )

    service.run_once(deadline=10.0, should_stop=lambda: False)

    planner.plan.assert_not_called()
    repository.get_details.assert_not_called()


def test_service_does_not_start_planner_when_deadline_is_exhausted() -> None:
    from app.memory.consolidation.service import MemoryConsolidationService

    memory_id = UUID("00000000-0000-4000-8000-000000000001")
    monotonic_clock = ControlledMonotonicClock(now=10.0)
    repository = DeadlineRepository(
        {memory_id: _detail_for_runtime(memory_id)}, monotonic_clock
    )
    planner = KeepPlanner()
    service = MemoryConsolidationService(
        repository=repository,
        planner=planner,
        privacy_reviewer=Mock(),
        batch_size=1,
        llm_timeout_seconds=15,
        clock=lambda: datetime(2026, 8, 23, tzinfo=UTC),
        monotonic_clock=monotonic_clock,
        model_id="gemma4:e4b",
        prompt_version="consolidation-v1",
        policy_version="policy-v1",
    )

    service.run_once(deadline=10.0, should_stop=lambda: False)

    assert planner.planned == []
    assert repository.applied == []
    assert set(repository.remaining) == {memory_id}


def test_service_caps_planner_timeout_at_the_configured_limit() -> None:
    from app.memory.consolidation.service import MemoryConsolidationService

    memory_id = UUID("00000000-0000-4000-8000-000000000001")
    monotonic_clock = ControlledMonotonicClock(now=10.0)
    repository = DeadlineRepository(
        {memory_id: _detail_for_runtime(memory_id)}, monotonic_clock
    )
    planner = KeepPlanner()
    service = MemoryConsolidationService(
        repository=repository,
        planner=planner,
        privacy_reviewer=Mock(),
        batch_size=1,
        llm_timeout_seconds=15,
        clock=lambda: datetime(2026, 8, 23, tzinfo=UTC),
        monotonic_clock=monotonic_clock,
        model_id="gemma4:e4b",
        prompt_version="consolidation-v1",
        policy_version="policy-v1",
    )

    service.run_once(deadline=100.0, should_stop=lambda: False)

    assert planner.timeouts == [15]


def _detail_for_runtime(memory_id: UUID) -> ApprovedMemoryDetail:
    from app.memory.persistence.contracts import MemorySourceInput, MemorySourceType

    return ApprovedMemoryDetail(
        memory=approved_memory(id=memory_id),
        sources=(
            MemorySourceInput(
                source_type=MemorySourceType.CONVERSATION_TURN,
                source_provider_id="core",
                source_ref=f"conversation:{memory_id}",
            ),
        ),
        lineage=(),
    )

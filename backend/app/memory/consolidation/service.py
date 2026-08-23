from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.memory.admission.contracts import (
    ApprovedMemoryCandidate,
    MemoryType,
    StructuredValue,
)
from app.memory.persistence.contracts import (
    ApprovedMemory,
    ApprovedMemoryDetail,
    ConsolidationConflictError,
    ConsolidationInputSnapshot,
    ConsolidationOperation,
    ConsolidationReceiptContext,
    FormationMethod,
    MemoryLineageInput,
    MemoryLineageRelation,
    MemorySourceInput,
    MemorySourceType,
    MemoryStatus,
    MemoryWriteContext,
    build_consolidation_idempotency_key,
)
from app.privacy.semantic.contracts import SemanticClassification

from .contracts import (
    ConsolidationPlan,
    ConsolidationPlanType,
    ConsolidationPrivacyReview,
    ConsolidationResponse,
)
from .selection import build_candidate_batches
from .validation import validate_plan


logger = logging.getLogger(__name__)


class ConsolidationRepository(Protocol):
    def list_character_ids(self) -> set[str]: ...

    def list_by_provider(
        self, *, character_id: str, provider_id: str, status: MemoryStatus
    ) -> list[ApprovedMemory]: ...

    def get_details(
        self, *, character_id: str, provider_id: str, memory_ids: tuple[UUID, ...]
    ) -> dict[UUID, ApprovedMemoryDetail]: ...

    def apply_consolidation(
        self,
        *,
        character_id: str,
        operation: ConsolidationOperation,
        inputs: tuple[ConsolidationInputSnapshot, ...],
        candidate: ApprovedMemoryCandidate | None,
        context: MemoryWriteContext | ConsolidationReceiptContext | None,
        canonical_memory_id: UUID | None,
        consolidated_at: datetime,
    ) -> ApprovedMemory: ...


class PlanProvider(Protocol):
    def plan(
        self,
        memories: tuple[ApprovedMemory, ...],
        *,
        timeout_seconds: float,
    ) -> ConsolidationResponse: ...


class PrivacyReviewer(Protocol):
    def review(
        self,
        *,
        memory_type: MemoryType,
        structured_value: StructuredValue,
        timeout_seconds: float,
    ) -> ConsolidationPrivacyReview: ...


class MemoryConsolidationService:
    def __init__(
        self,
        *,
        repository: ConsolidationRepository,
        planner: PlanProvider,
        privacy_reviewer: PrivacyReviewer,
        batch_size: int,
        llm_timeout_seconds: int,
        clock: Callable[[], datetime],
        monotonic_clock: Callable[[], float] = time.monotonic,
        model_id: str,
        prompt_version: str,
        policy_version: str,
        reprocess_interval_seconds: int = 3600,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._privacy_reviewer = privacy_reviewer
        self._batch_size = batch_size
        self._llm_timeout_seconds = llm_timeout_seconds
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._model_id = model_id
        self._prompt_version = prompt_version
        self._policy_version = policy_version
        self._reprocess_interval_seconds = reprocess_interval_seconds

    def run_once(self, *, deadline: float, should_stop: Callable[[], bool]) -> None:
        for character_id in sorted(self._repository.list_character_ids()):
            reprocess_before = self._clock() - timedelta(
                seconds=self._reprocess_interval_seconds
            )
            memories = tuple(
                memory
                for memory in self._repository.list_by_provider(
                    character_id=character_id,
                    provider_id="core",
                    status=MemoryStatus.ACTIVE,
                )
                if memory.last_consolidated_at is None
                or memory.last_consolidated_at <= reprocess_before
            )
            for batch in build_candidate_batches(memories, batch_size=self._batch_size):
                if should_stop() or self._monotonic_clock() >= deadline:
                    return
                planned = self._repository.get_details(
                    character_id=character_id,
                    provider_id="core",
                    memory_ids=tuple(memory.id for memory in batch),
                )
                if len(planned) != len(batch):
                    _log_outcome(
                        ConsolidationPlanType.NOOP,
                        "VERSION_CONFLICT",
                        len(batch),
                        self._model_id,
                        self._prompt_version,
                        self._policy_version,
                        0,
                    )
                    continue
                remaining_seconds = deadline - self._monotonic_clock()
                if should_stop() or remaining_seconds <= 0:
                    return
                response = self._planner.plan(
                    batch,
                    timeout_seconds=min(
                        self._llm_timeout_seconds,
                        remaining_seconds,
                    ),
                )
                for plan in response.plans:
                    if should_stop() or self._monotonic_clock() >= deadline:
                        return
                    plan_details = tuple(planned[ref.memory_id] for ref in plan.memories)
                    current_by_id = self._repository.get_details(
                        character_id=character_id,
                        provider_id="core",
                        memory_ids=tuple(ref.memory_id for ref in plan.memories),
                    )
                    current = tuple(
                        current_by_id[ref.memory_id]
                        for ref in plan.memories
                        if ref.memory_id in current_by_id
                    )
                    apply_validated_plan(
                        plan=plan,
                        planned=plan_details,
                        current=current,
                        repository=self._repository,
                        privacy_reviewer=self._privacy_reviewer,
                        consolidated_at=self._clock(),
                        model_id=self._model_id,
                        prompt_version=self._prompt_version,
                        policy_version=self._policy_version,
                        deadline=deadline,
                        monotonic_clock=self._monotonic_clock,
                    )


def apply_validated_plan(
    *,
    plan: ConsolidationPlan,
    planned: tuple[ApprovedMemoryDetail, ...],
    current: tuple[ApprovedMemoryDetail, ...],
    repository: ConsolidationRepository,
    privacy_reviewer: PrivacyReviewer,
    consolidated_at: datetime,
    model_id: str,
    prompt_version: str,
    policy_version: str,
    deadline: float,
    monotonic_clock: Callable[[], float],
) -> ConsolidationPlan:
    started_at = time.monotonic()
    validated = validate_plan(plan=plan, planned=planned, current=current)
    if validated.plan_type in {
        ConsolidationPlanType.NOOP,
        ConsolidationPlanType.CONFLICT,
    }:
        _log_terminal_plan(
            validated,
            model_id,
            prompt_version,
            policy_version,
            started_at,
        )
        return validated
    snapshots = tuple(
        ConsolidationInputSnapshot(
            memory_id=detail.memory.id,
            content_version=detail.memory.content_version,
            sources=detail.sources,
            lineage=detail.lineage,
        )
        for detail in current
    )
    candidate: ApprovedMemoryCandidate | None = None
    context: MemoryWriteContext | ConsolidationReceiptContext | None = None
    canonical_memory_id = validated.canonical_memory_id
    if validated.plan_type in {
        ConsolidationPlanType.MERGE,
        ConsolidationPlanType.SUPERSEDE,
    }:
        if validated.memory_type is None or validated.structured_value is None:
            raise RuntimeError("validated content plan is incomplete")
        remaining_seconds = deadline - monotonic_clock()
        if remaining_seconds <= 0:
            return _log_timeout(
                validated,
                model_id,
                prompt_version,
                policy_version,
                started_at,
            )
        review = privacy_reviewer.review(
            memory_type=validated.memory_type,
            structured_value=validated.structured_value,
            timeout_seconds=remaining_seconds,
        )
        if (
            review.candidate is None
            or review.assessment.classification
            is not SemanticClassification.NOT_SENSITIVE
            or review.assessment.policy_version != policy_version
        ):
            outcome = _as_noop(validated, review.assessment.reason_code.value)
            _log_terminal_plan(
                outcome,
                model_id,
                prompt_version,
                policy_version,
                started_at,
            )
            return outcome
        candidate = review.candidate
        relation = (
            MemoryLineageRelation.CONSOLIDATED_FROM
            if validated.plan_type is ConsolidationPlanType.MERGE
            else MemoryLineageRelation.SUPERSEDES
        )
        context = _write_context(
            validated,
            current,
            prompt_version=prompt_version,
            policy_version=policy_version,
            classifier_version=review.assessment.classifier_version,
            privacy_model_id=review.assessment.model_id,
            model_digest=review.assessment.model_digest,
            relation=relation,
        )
    elif validated.plan_type is ConsolidationPlanType.DELETE_EXACT_DUPLICATE:
        context = ConsolidationReceiptContext(
            build_consolidation_idempotency_key(
                character_id=current[0].memory.character_id,
                plan_type=validated.plan_type.value,
                memories=tuple(
                    (detail.memory.id, detail.memory.content_version)
                    for detail in current
                ),
                prompt_version=prompt_version,
            )
        )
    else:
        canonical_memory_id = current[0].memory.id
    if monotonic_clock() >= deadline:
        return _log_timeout(
            validated,
            model_id,
            prompt_version,
            policy_version,
            started_at,
        )
    try:
        repository.apply_consolidation(
            character_id=current[0].memory.character_id,
            operation=ConsolidationOperation(validated.plan_type.value),
            inputs=snapshots,
            candidate=candidate,
            context=context,
            canonical_memory_id=canonical_memory_id,
            consolidated_at=consolidated_at,
        )
    except (ConsolidationConflictError, LookupError):
        outcome = _as_noop(validated, "VERSION_CONFLICT")
        _log_terminal_plan(
            outcome, model_id, prompt_version, policy_version, started_at
        )
        return outcome
    _log_terminal_plan(
        validated, model_id, prompt_version, policy_version, started_at
    )
    return validated


def _log_timeout(
    plan: ConsolidationPlan,
    model_id: str,
    prompt_version: str,
    policy_version: str,
    started_at: float,
) -> ConsolidationPlan:
    outcome = _as_noop(plan, "TIMEOUT")
    _log_terminal_plan(
        outcome,
        model_id,
        prompt_version,
        policy_version,
        started_at,
    )
    return outcome


def _write_context(
    plan: ConsolidationPlan,
    current: tuple[ApprovedMemoryDetail, ...],
    *,
    prompt_version: str,
    policy_version: str,
    classifier_version: str,
    privacy_model_id: str,
    model_digest: str,
    relation: MemoryLineageRelation,
) -> MemoryWriteContext:
    basis = max(current, key=lambda detail: detail.memory.stated_at).memory
    return MemoryWriteContext(
        formation_method=FormationMethod.CONSOLIDATED,
        idempotency_key=build_consolidation_idempotency_key(
            character_id=basis.character_id,
            plan_type=plan.plan_type.value,
            memories=tuple(
                (detail.memory.id, detail.memory.content_version) for detail in current
            ),
            prompt_version=prompt_version,
        ),
        occurred_at=basis.occurred_at,
        occurred_timezone=basis.occurred_timezone,
        occurred_precision=basis.occurred_precision,
        stated_at=basis.stated_at,
        expires_at=basis.expires_at,
        policy_version=policy_version,
        classifier_version=classifier_version,
        model_id=privacy_model_id,
        model_digest=model_digest,
        prompt_version=prompt_version,
        sources=tuple(
            MemorySourceInput(
                source_type=MemorySourceType.CONSOLIDATION,
                source_provider_id="core",
                source_ref=str(detail.memory.id),
            )
            for detail in current
        ),
        lineage=tuple(
            MemoryLineageInput(
                related_memory_id=detail.memory.id,
                relation=relation,
            )
            for detail in current
        ),
    )


def _as_noop(plan: ConsolidationPlan, reason_code: str) -> ConsolidationPlan:
    return ConsolidationPlan(
        plan_type=ConsolidationPlanType.NOOP,
        reason_code=reason_code,
        memories=plan.memories,
    )


def _log_terminal_plan(
    plan: ConsolidationPlan,
    model_id: str,
    prompt_version: str,
    policy_version: str,
    started_at: float,
) -> None:
    latency_ms = max(0, round((time.monotonic() - started_at) * 1_000))
    if plan.plan_type is ConsolidationPlanType.CONFLICT:
        logger.info(
            "Memory consolidation completed: plan_type=%s reason_code=%s "
            "memory_ids=%s latency_ms=%d model_id=%s prompt_version=%s "
            "policy_version=%s",
            plan.plan_type.value,
            plan.reason_code,
            ",".join(str(ref.memory_id) for ref in plan.memories),
            latency_ms,
            model_id,
            prompt_version,
            policy_version,
        )
        return
    _log_outcome(
        plan.plan_type,
        plan.reason_code,
        len(plan.memories),
        model_id,
        prompt_version,
        policy_version,
        latency_ms,
    )


def _log_outcome(
    plan_type: ConsolidationPlanType,
    reason_code: str,
    memory_count: int,
    model_id: str,
    prompt_version: str,
    policy_version: str,
    latency_ms: int,
) -> None:
    logger.info(
        "Memory consolidation completed: memory_count=%d plan_type=%s "
        "reason_code=%s latency_ms=%d model_id=%s prompt_version=%s "
        "policy_version=%s",
        memory_count,
        plan_type.value,
        reason_code,
        latency_ms,
        model_id,
        prompt_version,
        policy_version,
    )

from __future__ import annotations

from dataclasses import replace

from app.memory.persistence.contracts import (
    ApprovedMemoryDetail,
    MemoryStatus,
)

from .contracts import ConsolidationPlan, ConsolidationPlanType


def validate_plan(
    *,
    plan: ConsolidationPlan,
    planned: tuple[ApprovedMemoryDetail, ...],
    current: tuple[ApprovedMemoryDetail, ...],
) -> ConsolidationPlan:
    if len(planned) != len(current) or not planned:
        return _noop(plan, "VERSION_CONFLICT")
    planned_by_id = {detail.memory.id: detail for detail in planned}
    current_by_id = {detail.memory.id: detail for detail in current}
    plan_ids = {ref.memory_id for ref in plan.memories}
    if plan_ids != set(planned_by_id) or plan_ids != set(current_by_id):
        return _noop(plan, "VERSION_CONFLICT")
    baseline_character = planned[0].memory.character_id
    baseline_kind = planned[0].memory.memory_kind
    baseline_type = planned[0].memory.memory_type
    for ref in plan.memories:
        before = planned_by_id[ref.memory_id]
        authoritative = current_by_id[ref.memory_id]
        memory = authoritative.memory
        if memory.character_id != baseline_character:
            return _noop(plan, "CHARACTER_BOUNDARY")
        if memory.provider_id != "core":
            return _noop(plan, "PROVIDER_BOUNDARY")
        if memory.memory_kind != baseline_kind or memory.memory_type != baseline_type:
            return _noop(plan, "MEMORY_TYPE_BOUNDARY")
        if memory.status is not MemoryStatus.ACTIVE:
            return _noop(plan, "STATUS_CONFLICT")
        if (
            memory.content_version != ref.content_version
            or memory.content_version != before.memory.content_version
        ):
            return _noop(plan, "VERSION_CONFLICT")
        if authoritative.sources != before.sources:
            return _noop(plan, "SOURCES_CONFLICT")
        if authoritative.lineage != before.lineage:
            return _noop(plan, "LINEAGE_CONFLICT")
    if plan.memory_type is not None and plan.memory_type is not baseline_type:
        return _noop(plan, "MEMORY_TYPE_BOUNDARY")
    if (
        plan.plan_type is ConsolidationPlanType.DELETE_EXACT_DUPLICATE
        and not is_exact_duplicate(current)
    ):
        return _noop(plan, "NOT_EXACT_DUPLICATE")
    return plan


def is_exact_duplicate(details: tuple[ApprovedMemoryDetail, ...]) -> bool:
    if len(details) < 2:
        return False
    baseline = _exact_content(details[0])
    return all(_exact_content(detail) == baseline for detail in details[1:])


def _exact_content(detail: ApprovedMemoryDetail) -> tuple[object, ...]:
    memory = detail.memory
    return (
        memory.character_id,
        memory.provider_id,
        memory.memory_kind,
        memory.memory_type,
        memory.structured_value,
        memory.normalized_text,
        memory.status,
        memory.occurred_at,
        memory.occurred_timezone,
        memory.occurred_precision,
        memory.stated_at,
        memory.expires_at,
        memory.last_user_mentioned_at,
        detail.sources,
        detail.lineage,
    )


def _noop(plan: ConsolidationPlan, reason_code: str) -> ConsolidationPlan:
    return replace(
        plan,
        plan_type=ConsolidationPlanType.NOOP,
        reason_code=reason_code,
        memory_type=None,
        structured_value=None,
        canonical_memory_id=None,
    )

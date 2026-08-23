from __future__ import annotations

from collections import defaultdict

from app.memory.persistence.contracts import ApprovedMemory, MemoryStatus


def build_candidate_batches(
    memories: tuple[ApprovedMemory, ...], *, batch_size: int
) -> tuple[tuple[ApprovedMemory, ...], ...]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    groups: dict[tuple[str, str], list[ApprovedMemory]] = defaultdict(list)
    for memory in memories:
        if memory.provider_id != "core" or memory.status is not MemoryStatus.ACTIVE:
            continue
        groups[(memory.character_id, memory.memory_type.value)].append(memory)
    batches: list[tuple[ApprovedMemory, ...]] = []
    for key in sorted(groups):
        ordered = sorted(
            groups[key],
            key=lambda item: (
                item.last_consolidated_at is not None,
                item.last_consolidated_at or item.created_at,
                item.created_at,
                str(item.id),
            ),
        )
        batches.extend(
            tuple(ordered[index : index + batch_size])
            for index in range(0, len(ordered), batch_size)
        )
    return tuple(batches)

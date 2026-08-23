from datetime import UTC, datetime
from uuid import UUID

from app.memory.admission.contracts import (
    MemoryType,
    PreferencePolarity,
    UserPreferenceValue,
)
from app.memory.memory_policy import resolved_memory_policy
from app.memory.persistence.contracts import (
    ApprovedMemory,
    MemoryStatus,
    TemporalPrecision,
)


DEFAULT_MEMORY_ID = UUID("00000000-0000-4000-8000-000000000042")


def approved_memory(**overrides: object) -> ApprovedMemory:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    values: dict[str, object] = {
        "id": DEFAULT_MEMORY_ID,
        "character_id": "miori",
        "provider_id": "core",
        "memory_kind": "SEMANTIC",
        "memory_type": MemoryType.USER_PREFERENCE,
        "structured_value": UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object="紅茶",
        ),
        "normalized_text": "SQLiteに保存された紅茶の好み",
        "policy_version": resolved_memory_policy().policy_version,
        "content_version": 1,
        "status": MemoryStatus.ACTIVE,
        "occurred_at": now,
        "occurred_timezone": "Asia/Tokyo",
        "occurred_precision": TemporalPrecision.SECOND,
        "stated_at": now,
        "expires_at": datetime(2999, 1, 1, tzinfo=UTC),
        "last_user_mentioned_at": None,
        "last_consolidated_at": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return ApprovedMemory(**values)  # type: ignore[arg-type]

"""検索・索引に必要な読み取り契約。旧記憶の更新・整理契約から分離する。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.memory.episodic.contracts import FiveW, RecordKind
from app.memory.persistence.contracts import ApprovedMemory, MemoryStatus, TemporalPrecision


@dataclass(frozen=True)
class EpisodicMemoryView:
    id: UUID
    character_id: str
    memory_type: RecordKind
    normalized_text: str
    five_w: FiveW | None
    policy_version: str
    content_version: int
    status: MemoryStatus
    created_at: datetime
    updated_at: datetime
    last_user_mentioned_at: datetime | None
    occurred_at: datetime | None
    occurred_timezone: str | None
    occurred_precision: TemporalPrecision | None
    provider_id: str = "core"
    memory_kind: str = "episodic"
    expires_at: datetime | None = None


ReadableMemory = ApprovedMemory | EpisodicMemoryView


class MemoryReadRepository(Protocol):
    def get(self, *, character_id: str, memory_id: UUID) -> ReadableMemory | None: ...

    def list_active(self, *, character_id: str) -> Sequence[ReadableMemory]: ...

    def list_character_ids(self) -> set[str]: ...

    def search_by_occurred_range(
        self, *, character_id: str, start: datetime, end: datetime,
        compatible_policy_versions: frozenset[str],
    ) -> Sequence[ReadableMemory]: ...

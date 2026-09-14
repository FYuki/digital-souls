"""意味記憶の現在の根拠・状態を毎回確認し、共通検索へ投影する。"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from app.memory.episodic.time_search import exact_occurred_at, matches_time
from app.memory.persistence.contracts import MemoryStatus, TemporalPrecision
from app.memory.read_contracts import MemoryReadRepository, ReadableMemory, SemanticMemoryView
from app.memory.semantic.contracts import FormationType, SemanticRecord, SemanticStatus
from app.memory.semantic.service import SemanticStore


class SemanticReadRepository:
    def __init__(self, store: SemanticStore) -> None:
        self.store = store

    def get(self, *, character_id: str, memory_id: UUID) -> SemanticMemoryView | None:
        records = self.store.reconcile(character_id)
        record = next((r for r in records if r.id == memory_id), None)
        return self._project(record, records) if record else None

    def list_active(self, *, character_id: str) -> list[SemanticMemoryView]:
        records = self.store.reconcile(character_id)
        return [view for record in records
                if (view := self._project(record, records)).status is MemoryStatus.ACTIVE]

    def list_character_ids(self) -> set[str]:
        with self.store.repository.read() as tx:
            return {str(row[0]) for row in tx.connection.execute("SELECT DISTINCT character_id FROM semantic_records")}

    def _project(self, record: SemanticRecord, records: Sequence[SemanticRecord]) -> SemanticMemoryView:
        value = record.proposition
        active = value is not None and record.status in {SemanticStatus.ACTIVE, SemanticStatus.HISTORICAL}
        if active and value is not None and record.formation_type is FormationType.EXPERIENCE_DERIVED:
            # 正本の一般化はACTIVEのまま保持する。MVPの通常会話では食い違う自己申告を採用する。
            active = not any(
                other.status is SemanticStatus.ACTIVE and other.proposition is not None
                and other.proposition.self_report
                and other.proposition.subject == value.subject
                and other.proposition.predicate == value.predicate
                and other.proposition.value != value.value
                for other in records
            )
        text = value.content if active and value else ""
        if text and record.status is SemanticStatus.HISTORICAL:
            text = "過去の状態（現在の状態として使わない）: " + text
        when = value.valid_from if value else None
        precision = when.parts.precision if when else None
        return SemanticMemoryView(
            id=record.id, character_id=record.character_id, memory_type=record.formation_type,
            normalized_text=text, proposition=value if active else None,
            policy_version=record.stamp.policy_version, content_version=record.content_version,
            status=MemoryStatus.ACTIVE if active else MemoryStatus.INACTIVE,
            current_self_report=bool(record.status is SemanticStatus.ACTIVE
                and record.formation_type is FormationType.DIRECT_EXTRACTION and value and value.self_report),
            created_at=record.created_at, updated_at=record.updated_at,
            last_user_mentioned_at=max((s.span.stated_at for s in record.sources if s.span and s.span.role=="user"),
                                       default=None),
            occurred_at=exact_occurred_at(when), occurred_timezone=when.timezone if when else None,
            occurred_precision=TemporalPrecision(precision) if precision in {p.value for p in TemporalPrecision} else None,
        )

    def search_by_occurred_range(
        self, *, character_id: str, start: datetime, end: datetime,
        compatible_policy_versions: frozenset[str],
    ) -> list[SemanticMemoryView]:
        return [r for r in self.list_active(character_id=character_id)
                if r.policy_version in compatible_policy_versions and r.proposition is not None
                and matches_time(r.proposition.valid_from, start, end)]


class WithSemanticReadRepository:
    def __init__(self, existing: MemoryReadRepository) -> None:
        self.existing = existing
        self._semantic: SemanticReadRepository | None = None

    def bind(self, semantic: SemanticReadRepository) -> None:
        if self._semantic is not None:
            raise RuntimeError("semantic reader already initialized")
        self._semantic = semantic

    @property
    def semantic(self) -> SemanticReadRepository:
        if self._semantic is None:
            raise RuntimeError("semantic reader is not initialized")
        return self._semantic

    def get(self, *, character_id: str, memory_id: UUID) -> ReadableMemory | None:
        semantic = self.semantic.get(character_id=character_id, memory_id=memory_id)
        if semantic is not None:
            return semantic
        return self.existing.get(character_id=character_id, memory_id=memory_id)

    def list_active(self, *, character_id: str) -> list[ReadableMemory]:
        return [*self.existing.list_active(character_id=character_id),
                *self.semantic.list_active(character_id=character_id)]

    def list_character_ids(self) -> set[str]:
        return self.existing.list_character_ids() | self.semantic.list_character_ids()

    def search_by_occurred_range(
        self, *, character_id: str, start: datetime, end: datetime,
        compatible_policy_versions: frozenset[str],
    ) -> list[ReadableMemory]:
        return [*self.existing.search_by_occurred_range(character_id=character_id, start=start, end=end,
                    compatible_policy_versions=compatible_policy_versions),
                *self.semantic.search_by_occurred_range(character_id=character_id, start=start, end=end,
                    compatible_policy_versions=compatible_policy_versions)]

"""Episode/Factの有効な正本を既存記憶と併せて検索へ渡す。"""

from collections import defaultdict, deque
from datetime import datetime
import sqlite3
from uuid import UUID

from app.memory.episodic.contracts import Record, RecordKind, RecordStatus, SourceSpan
from app.memory.episodic.rendering import render_five_w
from app.memory.episodic.repository import EpisodicRepository, EpisodicTransaction, RecordVersion
from app.memory.episodic.sources import (
    ConversationSourceGuard, InvalidConversationSource, validate_conversation_sources,
)
from app.memory.episodic.time_search import exact_occurred_at, matches_time
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.contracts import MemoryStatus, TemporalPrecision
from app.memory.read_contracts import EpisodicMemoryView, ReadableMemory


class EpisodicReadRepository:
    def __init__(self, repository: EpisodicRepository, source_guard: ConversationSourceGuard) -> None:
        self.repository = repository
        self.source_guard = source_guard

    def get(self, *, character_id: str, memory_id: UUID) -> EpisodicMemoryView | None:
        with self.source_guard.snapshot() as (history, cutoff), self.repository.read() as tx:
            record = tx.get(character_id, memory_id)
            return None if record is None else self._project(
                tx, history, cutoff, record, self._unsafe_facts(tx, history, cutoff, character_id),
            )

    def list_active(self, *, character_id: str) -> list[EpisodicMemoryView]:
        with self.source_guard.snapshot() as (history, cutoff), self.repository.read() as tx:
            unsafe = self._unsafe_facts(tx, history, cutoff, character_id)
            views = [self._project(tx, history, cutoff, record, unsafe)
                     for record in tx.list_records(character_id)]
        return [view for view in views if view.status is MemoryStatus.ACTIVE]

    def list_character_ids(self) -> set[str]:
        with self.repository.read() as tx:
            return tx.list_character_ids()

    def _sources_valid(
        self, history: sqlite3.Connection, cutoff: datetime, record: Record,
        sources: tuple[SourceSpan, ...],
    ) -> bool:
        if not sources or any(source.role == "activity" for source in sources):
            # Activity由来の実抽出は#249。出典検証入口なしに会話出典として扱わない。
            return False
        conversation_sources = tuple(s for s in sources if s.role in {"user", "assistant"})
        if not conversation_sources:
            return all(s.role == "manual" for s in sources)
        if record.conversation_id is None:
            return False
        try:
            validate_conversation_sources(
                history, character_id=record.character_id, conversation_id=record.conversation_id,
                sources=conversation_sources, cutoff=cutoff,
            )
        except InvalidConversationSource:
            return False
        return True

    def _current(
        self, tx: EpisodicTransaction, history: sqlite3.Connection, cutoff: datetime, record: Record,
    ) -> RecordVersion | None:
        if record.status is not RecordStatus.ACTIVE or record.five_w is None:
            return None
        versions = tx.versions(record.character_id, record.id)
        version = next((v for v in versions if v.content_version == record.content_version), None)
        if version is None or not self._sources_valid(history, cutoff, record, version.sources):
            return None
        return version

    def _unsafe_facts(
        self, tx: EpisodicTransaction, history: sqlite3.Connection, cutoff: datetime, character_id: str,
    ) -> set[UUID]:
        records = {r.id: r for r in tx.list_records(
            character_id, kind=RecordKind.FACT, active_only=False,
        )}
        blocked = {r.id for r in records.values() if self._current(tx, history, cutoff, r) is None}
        deleted = {r.id for r in records.values() if r.status is RecordStatus.DELETED}
        deletion_edges: dict[UUID, set[UUID]] = defaultdict(set)
        dependency_edges: dict[UUID, set[UUID]] = defaultdict(set)
        for merge in tx.merges(character_id):
            source, target = records.get(merge.source_fact_id), records.get(merge.target_fact_id)
            if source is None or target is None:
                continue
            if source.content_version == merge.source_version:
                deletion_edges[target.id].add(source.id)
                dependency_edges[target.id].add(source.id)
                if (not merge.valid or target.content_version != merge.target_version
                        or not self._sources_valid(history, cutoff, source, merge.evidence)):
                    blocked.add(source.id)
            if target.content_version == merge.target_version:
                deletion_edges[source.id].add(target.id)
        # 削除内容は同一性のある別IDからも使わない。一方、単なるsource失効で
        # 独立した有効出典を持つ統合先まで無効扱いにはしない。
        for seed, edges in ((deleted, deletion_edges), (blocked, dependency_edges)):
            pending = deque(seed)
            while pending:
                for dependent in edges[pending.popleft()]:
                    if dependent not in seed:
                        seed.add(dependent)
                        pending.append(dependent)
            blocked.update(seed)
        return blocked

    def _project(
        self, tx: EpisodicTransaction, history: sqlite3.Connection, cutoff: datetime, record: Record,
        unsafe_facts: set[UUID],
    ) -> EpisodicMemoryView:
        version = self._current(tx, history, cutoff, record)
        if version is not None and record.kind is RecordKind.EPISODE:
            # Factの旧内容を別Episode本文から検索し直さない。経験の監査レコードは残す。
            for link in tx.references(record.character_id, record.id):
                if link.episode_id != record.id or link.episode_version != record.content_version:
                    continue
                fact = tx.get(record.character_id, link.fact_id)
                if (not link.valid or fact is None or fact.id in unsafe_facts
                        or fact.content_version != link.fact_version
                        or self._current(tx, history, cutoff, fact) is None
                        or not self._sources_valid(history, cutoff, record, link.sources)):
                    version = None
                    break
        if version is not None and record.kind is RecordKind.FACT:
            # 有効な統合は代表だけを返す。失効した統合元は再評価するまで復帰させない。
            if record.id in unsafe_facts or any(
                m.source_fact_id == record.id and m.source_version == record.content_version
                for m in tx.merges(record.character_id)
            ):
                version = None
        value = record.five_w if version is not None else None
        when = value.when if value else None
        precision = when.parts.precision if when else None
        return EpisodicMemoryView(
            id=record.id, character_id=record.character_id, memory_type=record.kind,
            normalized_text=render_five_w(value, kind=record.kind) if value else "",
            five_w=value, policy_version=version.stamp.policy_version if version else "",
            content_version=record.content_version,
            status=MemoryStatus.ACTIVE if value else MemoryStatus.INACTIVE,
            created_at=record.created_at, updated_at=record.updated_at,
            last_user_mentioned_at=(max((s.stated_at for s in version.sources if s.role == "user"),
                                       default=None) if version else None),
            occurred_at=exact_occurred_at(when), occurred_timezone=when.timezone if when else None,
            occurred_precision=(TemporalPrecision(precision)
                                if precision is not None and precision in {p.value for p in TemporalPrecision} else None),
        )


class CombinedMemoryReadRepository:
    """読み取り専用の合成。旧preferenceの保存・consolidationへ新モデルを渡さない。"""

    def __init__(self, legacy: ApprovedMemoryRepository, episodic: EpisodicReadRepository) -> None:
        self.legacy = legacy
        self.episodic = episodic

    def get(self, *, character_id: str, memory_id: UUID) -> ReadableMemory | None:
        episodic = self.episodic.get(character_id=character_id, memory_id=memory_id)
        return episodic if episodic is not None else self.legacy.get(
            character_id=character_id, memory_id=memory_id,
        )

    def list_active(self, *, character_id: str) -> list[ReadableMemory]:
        return [*self.legacy.list_active(character_id=character_id),
                *self.episodic.list_active(character_id=character_id)]

    def list_character_ids(self) -> set[str]:
        return self.legacy.list_character_ids() | self.episodic.list_character_ids()

    def search_by_occurred_range(
        self, *, character_id: str, start: datetime, end: datetime,
        compatible_policy_versions: frozenset[str],
    ) -> list[ReadableMemory]:
        legacy = self.legacy.search_by_occurred_range(
            character_id=character_id, start=start, end=end,
            compatible_policy_versions=compatible_policy_versions,
        )
        episodic = [view for view in self.episodic.list_active(character_id=character_id)
                    if view.policy_version in compatible_policy_versions and view.five_w is not None
                    and matches_time(view.five_w.when, start, end)]
        return [*legacy, *episodic]

"""Fact単位の管理操作。外部privacy判定とSQLiteの原子的な版更新を分ける。"""

from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid5

from app.memory.episodic.contracts import FiveW, Person, Place, Record, RecordKind, RecordStatus, SourceSpan
from app.memory.episodic.read_repository import EpisodicReadRepository
from app.memory.episodic.registration import PrivacyReviewer
from app.memory.episodic.rendering import render_five_w
from app.memory.episodic.repository import EpisodicTransaction, RecordConflict
from app.memory.episodic.source_masks import overlaps_mask
from app.memory.persistence.contracts import MemoryStatus
from app.memory.providers import MemoryCorrectionRejected


class IndexPurger(Protocol):
    def delete_after_commit(self, *, character_id: str, memory_id: UUID) -> None: ...


def _manual_source(key: UUID, now: datetime) -> tuple[SourceSpan, ...]:
    return (SourceSpan(source_id=key, revision=1, role="manual", start=0, end=1, stated_at=now),)


def _fact(tx: EpisodicTransaction, character_id: str, record_id: UUID, expected: int) -> Record:
    record = tx.get(character_id, record_id)
    if record is None:
        raise LookupError("record was not found")
    if record.kind is not RecordKind.FACT:
        raise ValueError("only Facts can be managed")
    if record.content_version != expected or record.status is RecordStatus.DELETED:
        raise RecordConflict("Fact version or status has changed")
    return record


class EpisodicMemoryManagement:
    def __init__(
        self, *, reader: EpisodicReadRepository, reviewer: PrivacyReviewer,
        clock: Callable[[], datetime], index_sync: IndexPurger,
    ) -> None:
        self._reader = reader
        self._repository = reader.repository
        self._reviewer = reviewer
        self._clock = clock
        self._index_sync = index_sync

    def list(self, *, character_id: str) -> list[dict[str, object]]:
        with self._reader.source_guard.snapshot() as (history, cutoff), self._repository.read() as tx:
            unsafe = self._reader._unsafe_facts(tx, history, cutoff, character_id)
            results = []
            all_records = tx.list_records(character_id, active_only=False)
            views = {record.id: self._reader._project(
                tx, history, cutoff, record, unsafe, include_merged=True,
            ) for record in all_records}
            active = {record.id: record for record in all_records
                      if views[record.id].status is MemoryStatus.ACTIVE}
            merges = tx.merges(character_id)
            links = [link for episode in all_records if episode.kind is RecordKind.EPISODE
                     for link in tx.references(character_id, episode.id)]
            by_id = {record.id: record for record in all_records}
            for record in all_records:
                view = views[record.id]
                item = record.model_dump(mode="json")
                item["stored_status"] = record.status.value
                item["status"] = (RecordStatus.DELETED.value if record.status is RecordStatus.DELETED
                                  else view.status.value)
                item["five_w"] = view.five_w.model_dump(mode="json") if view.five_w else None
                item["normalized_text"] = view.normalized_text
                item["experienced_when"] = (
                    record.five_w.when.model_dump(mode="json")
                    if record.kind is RecordKind.EPISODE and record.five_w and record.five_w.when else None
                )
                # 旧版の本文を監査応答から返さない。ID・版・出典・判断の設定は追跡可能にする。
                item["versions"] = [{
                    "content_version": version.content_version,
                    "sources": [source.model_dump(mode="json") for source in version.sources],
                    "stamp": version.stamp.model_dump(mode="json"),
                    "created_at": version.created_at.isoformat(),
                    "content_erased": version.five_w is None,
                } for version in tx.versions(character_id, record.id)]
                references = []
                for link in links:
                    if record.id not in {link.episode_id, link.fact_id}:
                        continue
                    current_episode, current_fact = active.get(link.episode_id), active.get(link.fact_id)
                    data = link.model_dump(mode="json")
                    data["valid"] = bool(
                        link.valid and current_episode and current_fact
                        and current_episode.content_version == link.episode_version
                        and current_fact.content_version == link.fact_version
                        and self._reader._sources_valid(history, cutoff, by_id[link.episode_id], link.sources)
                    )
                    references.append(data)
                item["references"] = references
                relations = []
                for merge in merges:
                    if record.id not in {merge.source_fact_id, merge.target_fact_id}:
                        continue
                    source, target = active.get(merge.source_fact_id), active.get(merge.target_fact_id)
                    data = merge.model_dump(mode="json")
                    data["valid"] = bool(
                        merge.valid and source and target
                        and source.content_version == merge.source_version
                        and target.content_version == merge.target_version
                        and self._reader._sources_valid(history, cutoff, source, merge.evidence)
                    )
                    relations.append(data)
                item["merges"] = relations
                representative = tx.representative(character_id, record.id) if record.id in active else None
                item["representative_id"] = (
                    str(representative.id) if representative is not None and representative.id in active else None
                )
                results.append(item)
            return results

    def get(self, *, character_id: str, record_id: UUID) -> dict[str, object]:
        item = next((item for item in self.list(character_id=character_id) if item["id"] == str(record_id)), None)
        if item is None:
            raise LookupError("record was not found")
        return item

    def correct(
        self, *, character_id: str, record_id: UUID, expected_version: int,
        five_w: FiveW, idempotency_key: UUID,
    ) -> None:
        now = self._clock()
        with self._repository.read() as tx:
            replay = tx.operation_result(character_id, idempotency_key, "UPDATE", record_id)
            if replay is not None:
                return
            previous = _fact(tx, character_id, record_id, expected_version)
        assert previous.five_w is not None
        # 管理APIで新たな同一実体を偽装しない。既存IDの名前変更はIDを外して送る。
        old_entities = {person.entity_id: person.name for person in previous.five_w.who if person.entity_id}
        if previous.five_w.where and previous.five_w.where.entity_id:
            old_entities[previous.five_w.where.entity_id] = previous.five_w.where.name
        entities: list[Person | Place] = [*five_w.who, *([five_w.where] if five_w.where else [])]
        if any(entity.entity_id and old_entities.get(entity.entity_id) != entity.name for entity in entities):
            raise ValueError("entity identity was not offered for this name")
        if five_w.when is not None and five_w.when != previous.five_w.when:
            five_w = five_w.model_copy(update={"when": five_w.when.model_copy(update={"reference_at": now})})
        review = self._reviewer.review(
            kind=RecordKind.FACT, value=five_w,
            source_texts=(render_five_w(five_w, kind=RecordKind.FACT),),
        )
        if not review.allowed or review.stamp is None:
            raise MemoryCorrectionRejected(reason_code=review.reason)
        with self._repository.transaction(now=now) as tx:
            replay = tx.operation_result(character_id, idempotency_key, "UPDATE", record_id)
            if replay is None:
                current = _fact(tx, character_id, record_id, expected_version)
                if current != previous:
                    raise RecordConflict("Fact changed during privacy assessment")
                tx.update(
                    character_id=character_id, record_id=record_id, expected_version=expected_version,
                    five_w=five_w, sources=_manual_source(idempotency_key, now),
                    stamp=review.stamp, receipt_id=idempotency_key,
                )
        # 正本の版照合が即時に旧索引を無効化する。新索引は永続outboxで再生成する。
        self._index_sync.delete_after_commit(character_id=character_id, memory_id=record_id)

    def delete(
        self, *, character_id: str, record_id: UUID, expected_version: int, idempotency_key: UUID,
    ) -> None:
        now = self._clock()
        affected: set[UUID] = set()
        with self._repository.transaction(now=now) as tx:
            replay = tx.operation_result(character_id, idempotency_key, "DELETE", record_id)
            if replay is not None:
                affected.add(record_id)
            else:
                _fact(tx, character_id, record_id, expected_version)
                records = {r.id: r for r in tx.list_records(character_id, active_only=False)}
                versions_by_id = {record.id: tx.versions(character_id, record.id)
                                  for record in records.values() if record.kind is RecordKind.FACT}
                source_dependents: dict[
                    tuple[UUID, int, str], list[tuple[tuple[UUID, int], SourceSpan]]
                ] = defaultdict(list)
                for fact_id, versions in versions_by_id.items():
                    for version in versions:
                        for source in version.sources:
                            if source.role in {"user", "assistant"}:
                                source_dependents[source.source_id, source.revision, source.role].append(
                                    ((fact_id, version.content_version), source))
                # 同一性はID全体でなく、統合した内容版の辺に沿って伝播させる。
                edges: dict[tuple[UUID, int], set[tuple[UUID, int]]] = defaultdict(set)
                for merge in tx.merges(character_id):
                    left = merge.source_fact_id, merge.source_version
                    right = merge.target_fact_id, merge.target_version
                    edges[left].add(right)
                    edges[right].add(left)
                erased = {(record_id, v.content_version) for v in tx.versions(character_id, record_id)}
                pending = deque(erased)
                deleted_ids = {record_id}
                while pending:
                    pair = pending.popleft()
                    related = set(edges[pair])
                    record = records[pair[0]]
                    versions = versions_by_id[record.id]
                    affected_sources = next(v.sources for v in versions if v.content_version == pair[1])
                    # 同じ出典範囲から作った別Factや返答由来の言い換えも、本文消去の対象とする。
                    for removed_source in affected_sources:
                        roles = ("user", "assistant") if removed_source.role == "user" else (removed_source.role,)
                        for role in roles:
                            for dependent, source in source_dependents[
                                removed_source.source_id, removed_source.revision, role
                            ]:
                                if overlaps_mask(source, (removed_source,)):
                                    related.add(dependent)
                    if record.status is not RecordStatus.DELETED and any(
                        overlaps_mask(source, affected_sources) for source in versions[-1].sources
                    ):
                        # 新版でも削除対象の出典を継承しているなら独立した訂正とは扱わない。
                        related.add((record.id, record.content_version))
                    if pair[1] == record.content_version and record.status is not RecordStatus.DELETED:
                        deleted_ids.add(record.id)
                        related.update((record.id, v.content_version) for v in tx.versions(character_id, record.id))
                    for neighbor in related - erased:
                        erased.add(neighbor)
                        pending.append(neighbor)
                for target_id in sorted(deleted_ids, key=str):
                    target = records[target_id]
                    key = idempotency_key if target_id == record_id else uuid5(idempotency_key, str(target_id))
                    tx.delete(character_id=character_id, record_id=target_id,
                              expected_version=target.content_version,
                              sources=_manual_source(idempotency_key, now), receipt_id=key)
                    affected.add(target_id)
                for target_id, erased_version in erased:
                    if target_id not in deleted_ids:
                        tx.erase_version_content(character_id, target_id, erased_version)
                for episode in records.values():
                    if episode.kind is not RecordKind.EPISODE or episode.status is RecordStatus.DELETED:
                        continue
                    if any((link.fact_id, link.fact_version) in erased
                           for link in tx.references(character_id, episode.id)):
                        tx.redact_episode(episode, sources=_manual_source(idempotency_key, now),
                                          receipt_id=uuid5(idempotency_key, str(episode.id)))
                        affected.add(episode.id)
        for target_id in affected:
            self._index_sync.delete_after_commit(character_id=character_id, memory_id=target_id)
        self._repository.truncate_wal()

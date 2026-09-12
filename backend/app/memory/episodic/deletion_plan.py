"""削除する内容版から、消去が必要な依存版を同一transactionで求める。"""

from collections import defaultdict, deque
from dataclasses import dataclass
from uuid import UUID

from app.memory.episodic.contracts import Record, RecordKind, RecordStatus, SourceSpan
from app.memory.episodic.repository import EpisodicTransaction
from app.memory.episodic.source_masks import overlaps_mask

VersionKey = tuple[UUID, int]


@dataclass(frozen=True)
class DeletionPlan:
    records: dict[UUID, Record]
    erased_versions: frozenset[VersionKey]
    deleted_facts: frozenset[UUID]
    redacted_episodes: frozenset[UUID]
    deleted_legacy: frozenset[UUID]


def plan_deletion(tx: EpisodicTransaction, character_id: str, record_id: UUID) -> DeletionPlan:
    records = {r.id: r for r in tx.list_records(character_id, active_only=False)}
    versions = {r.id: tx.versions(character_id, r.id) for r in records.values()}
    sources = {(record_id, version.content_version): version.sources
               for record_id, history in versions.items() for version in history}
    edges: dict[VersionKey, set[VersionKey]] = defaultdict(set)
    for merge in tx.merges(character_id):
        left = merge.source_fact_id, merge.source_version
        right = merge.target_fact_id, merge.target_version
        edges[left].add(right)
        edges[right].add(left)
    for record in records.values():
        if record.kind is RecordKind.EPISODE:
            for link in tx.references(character_id, record.id):
                edges[link.fact_id, link.fact_version].add((link.episode_id, link.episode_version))
    response_versions: dict[UUID, set[VersionKey]] = defaultdict(set)
    fact_sources: dict[tuple[UUID, int, str], list[tuple[VersionKey, SourceSpan]]] = defaultdict(list)
    for node, source_spans in sources.items():
        for source in source_spans:
            if source.role == "assistant":
                response_versions[source.source_id].add(node)
            if records[node[0]].kind is RecordKind.FACT and source.role in {"user", "assistant"}:
                fact_sources[source.source_id, source.revision, source.role].append((node, source))
    legacy_current = tx.legacy_versions(character_id)
    legacy_sources = tx.legacy_response_sources(character_id)
    legacy_by_turn: dict[UUID, set[VersionKey]] = defaultdict(set)
    for node, turn_ids in legacy_sources.items():
        for turn_id in turn_ids:
            legacy_by_turn[turn_id].add(node)
            response_versions[turn_id].add(node)
    for node, source_spans in sources.items():
        if records[node[0]].kind is RecordKind.FACT:
            for source in source_spans:
                if source.role in {"user", "assistant"}:
                    edges[node].update(legacy_by_turn[source.source_id])
    for turn_id, memory_id, version in tx.response_references(character_id):
        edges[memory_id, version].update(response_versions[turn_id])

    erased = {(record_id, version.content_version) for version in versions[record_id]}
    pending = deque(erased)
    while pending:
        node = pending.popleft()
        related = set(edges[node])
        if node in legacy_sources:
            for neighbor in related - erased:
                if neighbor in sources or neighbor in legacy_sources:
                    erased.add(neighbor)
                    pending.append(neighbor)
            continue
        record = records[node[0]]
        removed_sources = sources[node]
        if record.kind is RecordKind.FACT:
            # Episode全体の出典を逆向きにたどって、別の関連Factまで消去しない。
            for removed in removed_sources:
                roles = ("user", "assistant") if removed.role == "user" else (removed.role,)
                for role in roles:
                    for dependent, source in fact_sources[removed.source_id, removed.revision, role]:
                        if overlaps_mask(source, (removed,)):
                            related.add(dependent)
        if record.status is not RecordStatus.DELETED:
            current = record.id, record.content_version
            if any(overlaps_mask(source, removed_sources) for source in sources[current]):
                related.add(current)
            if node == current:
                # 最新本文を消去する場合は旧版への参照からも復活させない。
                related.update((record.id, version.content_version) for version in versions[record.id])
        for neighbor in related - erased:
            if neighbor in sources or neighbor in legacy_sources:
                erased.add(neighbor)
                pending.append(neighbor)

    current_ids = {record.id for record in records.values()
               if record.status is not RecordStatus.DELETED
               and (record.id, record.content_version) in erased}
    return DeletionPlan(
        records=records, erased_versions=frozenset(node for node in erased if node in sources),
        deleted_legacy=frozenset(memory_id for memory_id, version in legacy_current.items()
                                 if (memory_id, version) in erased),
        deleted_facts=frozenset(record_id for record_id in current_ids
                                if records[record_id].kind is RecordKind.FACT),
        redacted_episodes=frozenset(record_id for record_id in current_ids
                                    if records[record_id].kind is RecordKind.EPISODE),
    )

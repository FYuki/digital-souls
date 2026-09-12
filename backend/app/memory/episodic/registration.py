"""非同期抽出の検証と登録。外部推論を終えてから出典lockとSQLite transactionを取る。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
import json
import sqlite3
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from app.memory.episodic.contracts import (
    ExtractionIdentity, FiveW, MergeRelation, Person, Place, Record, RecordKind, RecordStatus,
    ResolvedTime, SourceSpan, TimeParts,
)
from app.memory.episodic.extraction_contracts import ExtractionBatch, ExtractedRecord
from app.memory.episodic.matching import same_five_w
from app.memory.episodic.privacy import PrivacyReview
from app.memory.episodic.quotes import (
    InvalidExtraction, distinct_sources, fragment_span, owns_anchor, resolve_quote,
)
from app.memory.episodic.repository import EpisodicRepository, RecordConflict
from app.memory.episodic.sources import validate_conversation_sources
from app.memory.episodic.temporal import resolve_time
from app.memory.formation.thread_chunks import ThreadChunk
from app.memory.formation.thread_queue import ThreadFormationQueue, ThreadSnapshot


class PrivacyReviewer(Protocol):
    def review(
        self, *, kind: RecordKind, value: FiveW, source_texts: tuple[str, ...],
    ) -> PrivacyReview: ...


@dataclass(frozen=True)
class PreparedRecord:
    proposal: ExtractedRecord
    anchor: SourceSpan
    sources: tuple[SourceSpan, ...]
    value: FiveW
    previous: Record | None
    review: PrivacyReview


@dataclass(frozen=True)
class PreparedBatch:
    snapshot: ThreadSnapshot
    chunk: ThreadChunk
    batch: ExtractionBatch
    records: tuple[PreparedRecord, ...]
    links: tuple[tuple[SourceSpan, ...], ...]
    merges: tuple[tuple[SourceSpan, ...], ...]


@dataclass(frozen=True)
class RegistrationResult:
    saved: int
    rejected: int
    replayed: int
    skipped: int = 0


def receipt_id(record: PreparedRecord, character_id: str, conversation_id: UUID) -> UUID:
    # 本文・5W hash・配列順・thread revisionをidentityにしない。
    anchor = record.anchor
    key = (character_id, str(conversation_id), record.proposal.kind.value,
           record.proposal.operation, str(record.previous.id) if record.previous else None,
           str(anchor.source_id), anchor.revision, anchor.role, anchor.start)
    return uuid5(NAMESPACE_URL, json.dumps(key, separators=(",", ":")))


class EpisodicRegistrationService:
    def __init__(
        self, *, repository: EpisodicRepository, queue: ThreadFormationQueue,
        reviewer: PrivacyReviewer, timezone: str, clock: Callable[[], datetime],
        entity_labels: Mapping[str, str] | None = None,
    ) -> None:
        ZoneInfo(timezone)
        self._repository = repository
        self._queue = queue
        self._reviewer = reviewer
        self._timezone = timezone
        self._clock = clock
        self._entity_labels = dict(entity_labels or {"speaker:user": "ユーザー"})

    def catalog(self, snapshot: ThreadSnapshot) -> tuple[Record, ...]:
        with self._repository.read() as tx:
            return tx.list_records(snapshot.lease.character_id,
                                   conversation_id=snapshot.lease.conversation_id, active_only=False)

    def extraction_context(
        self, snapshot: ThreadSnapshot, chunk: ThreadChunk,
    ) -> tuple[tuple[Record, ...], dict[UUID, tuple[SourceSpan, ...]], dict[str, list[list[int]]]]:
        character_id, conversation_id = snapshot.lease.character_id, snapshot.lease.conversation_id
        with self._repository.read() as tx:
            records = tx.list_records(character_id, conversation_id=conversation_id, active_only=False)
            provenance = {record.id: tx.versions(character_id, record.id)[-1].sources for record in records}
            progress: dict[str, list[list[int]]] = {}
            for fragment in chunk.fragments:
                span = fragment_span(fragment)
                key = f"{span.source_id}:{span.revision}:{span.role}"
                progress.setdefault(key, []).extend(
                    list(pair) for pair in tx.processed_ranges(character_id, conversation_id, span)
                    if list(pair) not in progress[key])
        return records, provenance, progress

    def reconcile_sources(self, snapshot: ThreadSnapshot) -> None:
        """出典が変わった記憶を止める。検索側も現在の履歴で別途検証する。"""
        current = {s.turn.turn_id: s.revision for s in snapshot.sources}
        with self._queue.guard(snapshot):
            with self._repository.transaction(now=self._clock()) as tx:
                for record in tx.list_records(snapshot.lease.character_id,
                                               conversation_id=snapshot.lease.conversation_id):
                    latest = tx.versions(record.character_id, record.id)[-1]
                    for source in latest.sources:
                        if source.role in {"user", "assistant"} and current.get(source.source_id) != source.revision:
                            tx.invalidate_source(record.character_id, source.source_id, source.revision)

    def prepare(
        self, snapshot: ThreadSnapshot, chunk: ThreadChunk, batch: ExtractionBatch,
        catalog: tuple[Record, ...], *, entity_labels: Mapping[str, str] | None = None,
        extraction_identity: ExtractionIdentity | None = None,
    ) -> PreparedBatch:
        if not batch.complete:
            raise InvalidExtraction("incomplete extraction cannot be registered")
        character_id = snapshot.lease.character_id
        conversation_id = snapshot.lease.conversation_id
        known = {r.id: r for r in catalog if r.character_id == character_id and r.conversation_id == conversation_id}
        entity_labels = dict(self._entity_labels) | dict(entity_labels or {})
        for record in known.values():
            if record.status is RecordStatus.ACTIVE and record.five_w is not None:
                for person in record.five_w.who:
                    if person.entity_id is not None:
                        entity_labels[person.entity_id] = person.name
                if record.five_w.where and record.five_w.where.entity_id:
                    entity_labels[record.five_w.where.entity_id] = record.five_w.where.name
        links = tuple(tuple(resolve_quote(q, snapshot, chunk) for q in link.sources) for link in batch.links)
        merges = tuple(tuple(resolve_quote(q, snapshot, chunk) for q in merge.evidence) for merge in batch.merges)
        related_sources: dict[str, tuple[SourceSpan, ...]] = {}
        for link, evidence in zip(batch.links, links, strict=True):
            for key in (link.episode, link.fact):
                related_sources[key] = related_sources.get(key, ()) + evidence
        for relation, evidence in zip(batch.merges, merges, strict=True):
            for key in (relation.source, relation.target):
                related_sources[key] = related_sources.get(key, ()) + evidence
        prepared: list[PreparedRecord] = []
        anchor_keys: set[tuple[RecordKind, str, UUID | None, UUID, int, str, int]] = set()
        for proposal in batch.records:
            anchor = resolve_quote(proposal.anchor, snapshot, chunk)
            if not owns_anchor(chunk, anchor):
                raise InvalidExtraction("record anchor is outside its owned range")
            anchor_key = (proposal.kind, proposal.operation, proposal.target.id if proposal.target else None,
                          anchor.source_id, anchor.revision, anchor.role, anchor.start)
            if anchor_key in anchor_keys:
                raise InvalidExtraction("multiple records share the same kind and evidence anchor")
            anchor_keys.add(anchor_key)
            previous = None
            if proposal.target is not None:
                previous = known.get(proposal.target.id)
                if (
                    previous is None or previous.kind is not proposal.kind
                    or previous.content_version != proposal.target.version
                    or previous.status is RecordStatus.DELETED
                ):
                    raise RecordConflict("extraction target is not a current in-scope record")
                if proposal.operation == "REFERENCE" and previous.status is not RecordStatus.ACTIVE:
                    raise RecordConflict("an inactive Fact cannot be referenced")
            sources = distinct_sources((anchor,) + tuple(
                resolve_quote(quote, snapshot, chunk) for quote in proposal.sources))
            if proposal.time_source:
                sources = distinct_sources(sources + (resolve_quote(proposal.time_source, snapshot, chunk),))
            sources = distinct_sources(sources + related_sources.get(proposal.key, ()))
            value = self._value(proposal, previous, anchor, snapshot, chunk, entity_labels)
            if previous is not None and previous.status is RecordStatus.ACTIVE:
                with self._repository.read() as tx:
                    sources = distinct_sources(tx.versions(character_id, previous.id)[-1].sources + sources)
            source_texts = self._source_texts(snapshot, sources)
            review = self._reviewer.review(kind=proposal.kind, value=value, source_texts=source_texts)
            if review.stamp is not None and extraction_identity is not None:
                review = replace(review, stamp=review.stamp.model_copy(update={"extraction": extraction_identity}))
            prepared.append(PreparedRecord(proposal, anchor, sources, value, previous, review))
        return PreparedBatch(snapshot, chunk, batch, tuple(prepared), links, merges)

    def _source_texts(self, snapshot: ThreadSnapshot, sources: tuple[SourceSpan, ...]) -> tuple[str, ...]:
        by_id = {s.turn.turn_id: s for s in snapshot.sources}
        texts: list[str] = []
        for span in sources:
            if span.role == "manual":
                continue
            source = by_id.get(span.source_id)
            if source is None or source.revision != span.revision:
                raise RecordConflict("record depends on an unavailable source")
            text = source.turn.user_content if span.role == "user" else source.turn.assistant_content
            if text is None:
                raise RecordConflict("source content is unavailable")
            texts.append(text)
        return tuple(dict.fromkeys(texts))

    def _value(
        self, proposal: ExtractedRecord, previous: Record | None, anchor: SourceSpan,
        snapshot: ThreadSnapshot, chunk: ThreadChunk, entity_labels: dict[str, str],
    ) -> FiveW:
        if proposal.operation == "REFERENCE":
            assert previous is not None and previous.five_w is not None
            return previous.five_w
        assert proposal.five_w is not None
        extracted = proposal.five_w
        when = None
        if proposal.kind is RecordKind.EPISODE:
            # 会話からのEpisodeは、対象の出来事が起きた日ではなく聞いた経験の時刻。
            local = anchor.stated_at.astimezone(ZoneInfo(self._timezone))
            when = ResolvedTime(
                parts=TimeParts(year=local.year, month=local.month, day=local.day,
                                hour=local.hour, minute=local.minute, second=local.second),
                timezone=self._timezone, reference_at=anchor.stated_at,
            )
            if previous is not None and previous.status is RecordStatus.ACTIVE and previous.five_w is not None:
                when = previous.five_w.when
        elif extracted.when is not None:
            assert proposal.time_source is not None
            time_source = resolve_quote(proposal.time_source, snapshot, chunk)
            when = resolve_time(extracted.when, stated_at=time_source.stated_at, timezone=self._timezone)
            if previous and previous.five_w and previous.five_w.when:
                old_time = previous.five_w.when
                if when.model_dump(exclude={"reference_at"}) == old_time.model_dump(exclude={"reference_at"}):
                    when = old_time
        people = []
        for index, person in enumerate(extracted.who):
            identity = self._entity_id(person.entity_id, person.name, entity_labels, anchor, f"person:{index}")
            people.append(Person(name=person.name, role=person.role, entity_id=identity))
        where = None
        if extracted.where:
            identity = self._entity_id(extracted.where.entity_id, extracted.where.name,
                                       entity_labels, anchor, "place")
            where = Place(name=extracted.where.name, entity_id=identity)
        value = FiveW(who=tuple(people), what=extracted.what, when=when, where=where,
                      why=extracted.why, context=extracted.context)
        if previous is not None and previous.status is RecordStatus.ACTIVE and previous.five_w is not None:
            replacement = previous.five_w.model_dump()
            new_fields = value.model_dump()
            for field in proposal.changes:
                if field == "when" and proposal.kind is RecordKind.EPISODE:
                    continue
                replacement[field] = new_fields[field]
            value = FiveW.model_validate(replacement)
        return value

    @staticmethod
    def _entity_id(
        identity: str | None, name: str, labels: dict[str, str], anchor: SourceSpan, slot: str,
    ) -> str:
        if identity is not None:
            if labels.get(identity) != name:
                raise InvalidExtraction("entity identity was not supplied with this name")
            return identity
        # 初出の実体には出典内だけのIDを付与する。同名というだけで既存実体へ統合しない。
        key = json.dumps((str(anchor.source_id), anchor.revision, anchor.role, anchor.start, slot))
        return "source-entity:" + str(uuid5(NAMESPACE_URL, key))

    def commit(self, prepared: PreparedBatch) -> RegistrationResult:
        snapshot = prepared.snapshot
        character_id, conversation_id = snapshot.lease.character_id, snapshot.lease.conversation_id
        saved = rejected = replayed = skipped = 0
        with self._queue.guard(snapshot) as history:
            for record in prepared.records:
                if record.review.allowed:
                    validate_conversation_sources(
                        history, character_id=character_id, conversation_id=conversation_id,
                        sources=tuple(s for s in record.sources if s.role in {"user", "assistant"}),
                        cutoff=snapshot.retention_cutoff,
                    )
            for sources in (*prepared.links, *prepared.merges):
                validate_conversation_sources(history, character_id=character_id, conversation_id=conversation_id,
                                               sources=sources, cutoff=snapshot.retention_cutoff)
            with self._repository.transaction(now=self._clock()) as tx:
                records: dict[str, Record] = {}
                changed_keys: set[str] = set()
                for item in prepared.records:
                    proposal, previous = item.proposal, item.previous
                    if not item.review.allowed:
                        rejected += 1
                        continue
                    assert item.review.stamp is not None
                    current = tx.get(character_id, previous.id) if previous else None
                    if previous is not None and current != previous:
                        raise RecordConflict("target changed while extraction or privacy assessment was running")
                    processed = tx.is_processed(character_id, conversation_id, item.anchor)
                    if processed and (current is None or current.status is RecordStatus.ACTIVE):
                        acknowledged = current or tx.receipt_record(
                            character_id, receipt_id(item, character_id, conversation_id))
                        if acknowledged is not None and acknowledged.status is RecordStatus.ACTIVE:
                            records[proposal.key] = acknowledged
                            replayed += 1
                        else:
                            skipped += 1
                        continue
                    if previous is None:
                        result = tx.create(
                            character_id=character_id, conversation_id=conversation_id, kind=proposal.kind,
                            five_w=item.value, sources=item.sources, stamp=item.review.stamp,
                            receipt_id=receipt_id(item, character_id, conversation_id),
                        )
                        records[proposal.key] = result.record
                        if not result.replayed:
                            changed_keys.add(proposal.key)
                        saved += not result.replayed
                        replayed += result.replayed
                    elif proposal.operation == "REFERENCE" or (
                        proposal.operation == "UPDATE" and item.value == previous.five_w
                    ):
                        records[proposal.key] = previous
                        replayed += 1
                    else:
                        result = tx.update(
                            character_id=character_id, record_id=previous.id,
                            expected_version=previous.content_version, five_w=item.value,
                            sources=item.sources, stamp=item.review.stamp,
                            receipt_id=receipt_id(item, character_id, conversation_id),
                        )
                        records[proposal.key] = result.record
                        if not result.replayed:
                            changed_keys.add(proposal.key)
                        saved += not result.replayed
                        replayed += result.replayed
                for link, sources in zip(prepared.batch.links, prepared.links, strict=True):
                    episode, fact = records.get(link.episode), records.get(link.fact)
                    if episode is None or fact is None:
                        continue
                    if not {link.episode, link.fact}.intersection(changed_keys) and all(
                        tx.is_processed(character_id, conversation_id, source) for source in sources
                    ):
                        continue
                    tx.add_reference(
                        character_id=character_id, episode_id=episode.id, episode_version=episode.content_version,
                        fact_id=fact.id, fact_version=fact.content_version, sources=distinct_sources(sources),
                    )
                for merge_proposal, evidence in zip(prepared.batch.merges, prepared.merges, strict=True):
                    source, target = records.get(merge_proposal.source), records.get(merge_proposal.target)
                    if source is None or target is None or source.id == target.id:
                        continue
                    proposed_targets = {
                        relation.target for relation in prepared.batch.merges
                        if relation.source == merge_proposal.source
                    }
                    if len(proposed_targets) != 1:
                        continue
                    if not {merge_proposal.source, merge_proposal.target}.intersection(changed_keys) and all(
                        tx.is_processed(character_id, conversation_id, item) for item in evidence
                    ):
                        continue
                    if source.five_w is None or target.five_w is None or not same_five_w(source.five_w, target.five_w):
                        continue
                    existing = [m for m in tx.merges(character_id) if m.valid and m.source_fact_id == source.id]
                    if existing:
                        continue
                    try:
                        tx.add_merge(MergeRelation(
                            id=uuid4(), character_id=character_id, source_fact_id=source.id,
                            source_version=source.content_version, target_fact_id=target.id,
                            target_version=target.content_version, conversation_id=conversation_id,
                            policy="same-thread-five-w-v1", evidence=distinct_sources(evidence), valid=True,
                        ))
                    except sqlite3.IntegrityError:
                        # 保存可能なFactと照合同定の見送りを分離する。循環等で本文を捨てない。
                        continue
                tx.mark_processed(character_id, conversation_id,
                                  tuple(fragment_span(p) for p in prepared.chunk.primary))
        return RegistrationResult(saved, rejected, replayed, skipped)

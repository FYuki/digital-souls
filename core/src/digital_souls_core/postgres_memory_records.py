"""Atomic PostgreSQL implementation of the canonical memory storage port."""

import hashlib
import json
from typing import Any

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from .application import CoreError
from .history import Binding, SourceReference
from .memory_confirmation import blocked, decode
from .memory_contracts import SourceVersion
from .memory_record_store import MemoryRecord, RecordBatch, RetrievalCandidate
from .memory_records import (
    Citation,
    Episode,
    EpisodeContext,
    EpisodeEvidence,
    EpisodeFactLink,
    Fact,
    FormationType,
    Proposition,
    RecordHead,
    RecordKind,
    RecordRef,
    RecordState,
    Semantic,
    Speaker,
)
from .postgres_db import PostgresDatabase, key
from .postgres_record_codec import (
    encode,
    five_w,
    normalized,
    projection,
    record_citations,
    ref,
    temporal,
)
from .postgres_record_schema import COLUMNS

TABLES = {
    RecordKind.EPISODE: "memory_episodes",
    RecordKind.FACT: "memory_fact_versions",
    RecordKind.SEMANTIC: "memory_semantics",
    RecordKind.EPISODE_FACT_LINK: "memory_episode_fact_links",
}


def rejected(code: str = "memory_record_invalid") -> CoreError:
    return CoreError(409, code, "Memory record is unavailable")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PostgresMemoryRecords:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database
        database.initialize()

    def _source(self, db: Connection[tuple[Any, ...]], binding: Binding, c: Citation) -> None:
        if c.binding != binding:
            raise rejected("memory_source_invalid")
        source = c.source.reference
        row = db.execute(
            "SELECT "
            "c.private_mode,c.memory_epoch,t.private_mode,t.memory_excluded,t."
            "messages,t.memory_confirmation "
            "FROM conversations c JOIN turns t ON t.binding=c.binding AND t.conversation=c.id "
            "WHERE c.binding=%s AND c.id=%s AND t.revision=%s",
            (key(binding), source.conversation_id, source.turn_revision),
        ).fetchone()
        try:
            if row is None or row[0] or row[2] or row[1] != c.source.epoch:
                raise rejected("memory_source_invalid")
            messages = json.loads(row[4])
            if (
                not 0 <= source.message_index < len(messages)
                or source.message_index in json.loads(row[3])
                or blocked(decode(row[5]), source.message_index)
            ):
                raise rejected("memory_source_invalid")
            message = messages[source.message_index]
            text = message.get("content")
            if (
                message["role"] != c.speaker.value
                or not isinstance(text, str)
                or not 0 <= c.start < c.end <= len(text)
            ):
                raise rejected("memory_source_invalid")
        except (ValueError, KeyError, TypeError):
            raise rejected("memory_source_invalid") from None

    def _row(
        self,
        db: Connection[tuple[Any, ...]],
        binding: Binding,
        kind: RecordKind,
        identifier: str,
        version: int | None = None,
    ) -> dict[str, Any] | None:
        table = TABLES[kind]
        query = sql.SQL("SELECT * FROM {} WHERE binding=%s AND id=%s").format(sql.Identifier(table))
        params: tuple[Any, ...] = (key(binding), identifier)
        if kind is RecordKind.FACT and version is None:
            query += sql.SQL(
                " AND version=(SELECT version FROM memory_facts WHERE binding=%s AND id=%s)"
            )
            params += (key(binding), identifier)
        elif version is not None:
            query += sql.SQL(" AND version=%s")
            params += (version,)
        row = db.execute(query, params).fetchone()
        return None if row is None else dict(zip(COLUMNS[table], row, strict=True))

    def _reference(
        self, db: Connection[tuple[Any, ...]], binding: Binding, reference: RecordRef
    ) -> dict[str, Any]:
        if reference.binding != binding:
            raise rejected()
        row = self._row(db, binding, reference.kind, reference.record_id, reference.version)
        if row is None or row["state"] != "active":
            raise rejected()
        return row

    def register(
        self, binding: Binding, batch: RecordBatch, formation_version: str
    ) -> tuple[RecordRef, ...]:
        records: tuple[MemoryRecord, ...] = (
            *batch.episodes,
            *(write.fact for write in batch.facts),
            *batch.links,
            *batch.semantics,
        )
        if not records or not isinstance(formation_version, str) or not formation_version.strip():
            raise rejected()
        refs = tuple(ref(r) for r in records)
        if len(set((r.kind, r.record_id) for r in refs)) != len(refs) or any(
            r.binding != binding or r.state is not RecordState.ACTIVE for r in records
        ):
            raise rejected()
        citations = tuple(set(c for r in records for c in record_citations(r)))
        with self.database.transaction(binding) as db:
            # Existing Episode evidence contributes its complete citations to the
            # registration identity, including reason citations, before any retry.
            evidence_citations: list[Citation] = []
            new_episodes = {e.episode_id: e for e in batch.episodes}
            for s in batch.semantics:
                for ev in s.episode_evidence:
                    if ev.reference.binding != binding:
                        raise rejected()
                    e = new_episodes.get(ev.reference.record_id)
                    if e is not None:
                        if ref(e) != ev.reference:
                            raise rejected()
                        ec = e.citations
                        evidence_citations.extend(record_citations(e))
                    else:
                        self._reference(db, binding, ev.reference)
                        ec = self._citations(db, binding, ev.reference)
                        evidence_citations.extend(
                            self._citations(db, binding, ev.reference, role=None)
                        )
                    if frozenset(c.source.reference for c in ec) != ev.sources:
                        raise rejected()
            all_citations = tuple(set((*citations, *evidence_citations)))
            identity_parts = [key(binding), normalized(all_citations), formation_version]
            if batch.links:
                # Named endpoints preserve each pair and its roles during normalization.
                endpoint_pairs = tuple(
                    {"episode": link.episode, "fact": link.fact} for link in batch.links
                )
                identity_parts.append(normalized(endpoint_pairs))
            identity = _hash(identity_parts)
            digest = _hash(normalized(batch))
            prior = db.execute(
                (
                    "SELECT request_digest,results FROM memory_record_registrations "
                    "WHERE binding=%s AND id=%s"
                ),
                (key(binding), identity),
            ).fetchone()
            if prior is not None:
                if prior[0] is None or prior[0] != digest:
                    raise rejected("memory_registration_conflict")
                result = tuple(
                    RecordRef(
                        kind=RecordKind(r["kind"]),
                        record_id=r["record_id"],
                        version=r["version"],
                        binding=binding,
                    )
                    for r in prior[1]
                )
                for r in result:
                    self._reference(db, binding, r)
                for c in all_citations:
                    self._source(db, binding, c)
                return result
            for c in all_citations:
                self._source(db, binding, c)
            for e in batch.episodes:
                self._new(db, e)
            for write in batch.facts:
                f = write.fact
                head = db.execute(
                    "SELECT version,state FROM memory_facts WHERE binding=%s AND id=%s",
                    (key(binding), f.fact_id),
                ).fetchone()
                if head is None:
                    if write.expected_version is not None or f.version != 1:
                        raise rejected("memory_version_conflict")
                    self._insert(db, "memory_facts", self._base(f))
                else:
                    if (
                        head[1] != "active"
                        or write.expected_version != head[0]
                        or type(write.expected_version) is not int
                        or f.version != head[0] + 1
                    ):
                        raise rejected("memory_version_conflict")
                    db.execute(
                        "UPDATE memory_facts SET version=%s WHERE binding=%s AND id=%s",
                        (f.version, key(binding), f.fact_id),
                    )
                self._insert_record(db, f)
            for link in batch.links:
                self._reference(db, binding, link.episode)
                self._reference(db, binding, link.fact)
                self._new(db, link)
            for s in batch.semantics:
                self._new(db, s)
                for ev in s.episode_evidence:
                    self._reference(db, binding, ev.reference)
                    self._insert(
                        db,
                        "memory_semantic_episodes",
                        {
                            "binding": key(binding),
                            "semantic": s.semantic_id,
                            "semantic_version": s.version,
                            "episode": ev.reference.record_id,
                            "episode_version": ev.reference.version,
                        },
                    )
            # Revocation erases the comparison digest, retaining only the key and
            # content-free output addresses to reject subsequent retries.
            self._insert(
                db,
                "memory_record_registrations",
                {
                    "binding": key(binding),
                    "id": identity,
                    "request_digest": digest,
                    "results": Jsonb(
                        [
                            {"kind": r.kind.value, "record_id": r.record_id, "version": r.version}
                            for r in refs
                        ]
                    ),
                },
            )
            return refs

    def _base(self, record: MemoryRecord) -> dict[str, Any]:
        reference = ref(record)
        return {
            "binding": key(record.binding),
            "id": reference.record_id,
            "version": record.version,
            "state": record.state.value,
            "created_at": record.created_at,
        }

    def _insert(self, db: Connection[tuple[Any, ...]], table: str, values: dict[str, Any]) -> None:
        db.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table),
                sql.SQL(",").join(map(sql.Identifier, values)),
                sql.SQL(",").join(sql.Placeholder() for _ in values),
            ),
            tuple(values.values()),
        )

    def _new(
        self, db: Connection[tuple[Any, ...]], record: Episode | Semantic | EpisodeFactLink
    ) -> None:
        reference = ref(record)
        if (
            record.version != 1
            or self._row(db, record.binding, reference.kind, reference.record_id) is not None
        ):
            raise rejected("memory_identity_conflict")
        self._insert_record(db, record)

    def _insert_record(self, db: Connection[tuple[Any, ...]], record: MemoryRecord) -> None:
        reference = ref(record)
        values = self._base(record)
        if isinstance(record, EpisodeFactLink):
            values.update(
                episode=record.episode.record_id,
                episode_version=record.episode.version,
                fact=record.fact.record_id,
                fact_version=record.fact.version,
            )
        else:
            values.update(
                normalized_text=record.normalized_text,
                last_user_mentioned_at=record.last_user_mentioned_at,
            )
            if isinstance(record, (Episode, Fact)):
                values["five_w"] = Jsonb(encode(record.five_w))
            if isinstance(record, Episode):
                name, time = "experience_time", record.experience_time
                values.update(experienced_at=record.experienced_at, context=record.context.value)
            elif isinstance(record, Fact):
                name, time = "target_time", record.target_time
            else:
                name, time = "applicability", record.applicability
                values.update(
                    formation_type=record.formation_type.value,
                    proposition=Jsonb(encode(record.proposition)),
                )
            # Validate all embedded times too; never accept unknown zone names.
            if isinstance(record, (Episode, Fact)) and record.five_w.when is not None:
                projection(record.five_w.when)
            start, end, precision = projection(time)
            values.update(
                {
                    name: Jsonb(encode(time)),
                    "time_start": start,
                    "time_end": end,
                    "time_precision": precision,
                }
            )
        self._insert(db, TABLES[reference.kind], values)
        if not isinstance(record, EpisodeFactLink):
            groups = [("record", record.citations)]
            if isinstance(record, (Episode, Fact)) and record.five_w.why is not None:
                groups.append(("reason", record.five_w.why.citations))
            for role, citations in groups:
                for c in dict.fromkeys(citations):
                    source = c.source.reference
                    owner = {
                        "episode": None,
                        "fact": None,
                        "semantic": None,
                        reference.kind.value: reference.record_id,
                    }
                    self._insert(
                        db,
                        "memory_record_citations",
                        {
                            "binding": key(record.binding),
                            "record_kind": reference.kind.value,
                            "record_id": reference.record_id,
                            "version": record.version,
                            **owner,
                            "conversation": source.conversation_id,
                            "revision": source.turn_revision,
                            "position": source.message_index,
                            "epoch": c.source.epoch,
                            "speaker": c.speaker.value,
                            "citation_role": role,
                            "start_offset": c.start,
                            "end_offset": c.end,
                        },
                    )

    def _citations(
        self,
        db: Connection[tuple[Any, ...]],
        binding: Binding,
        reference: RecordRef,
        *,
        role: str | None = "record",
    ) -> tuple[Citation, ...]:
        rows = db.execute(
            (
                "SELECT "
                "conversation,revision,position,epoch,speaker,start_offset,end_off"
                "set FROM memory_record_citations WHERE binding=%s AND "
                "record_kind=%s AND record_id=%s AND version=%s AND (%s::text IS "
                "NULL OR citation_role=%s) ORDER BY seq"
            ),
            (
                key(binding),
                reference.kind.value,
                reference.record_id,
                reference.version,
                role,
                role,
            ),
        ).fetchall()
        return tuple(
            Citation(binding, SourceVersion(SourceReference(c, r, p), e), Speaker(s), start, end)
            for c, r, p, e, s, start, end in rows
        )

    def _decode(
        self,
        db: Connection[tuple[Any, ...]],
        binding: Binding,
        kind: RecordKind,
        row: dict[str, Any],
    ) -> MemoryRecord:
        base = {k: row[k] for k in ("version", "state", "created_at")}
        base.update(binding=binding, state=RecordState(row["state"]))
        reference = RecordRef(
            kind=kind, record_id=row["id"], version=row["version"], binding=binding
        )
        if kind is RecordKind.EPISODE_FACT_LINK:
            return EpisodeFactLink(
                **base,
                link_id=row["id"],
                episode=RecordRef(
                    kind=RecordKind.EPISODE,
                    record_id=row["episode"],
                    version=row["episode_version"],
                    binding=binding,
                ),
                fact=RecordRef(
                    kind=RecordKind.FACT,
                    record_id=row["fact"],
                    version=row["fact_version"],
                    binding=binding,
                ),
            )
        base.update(
            normalized_text=row["normalized_text"],
            last_user_mentioned_at=row["last_user_mentioned_at"],
            citations=self._citations(db, binding, reference),
        )
        if kind is RecordKind.EPISODE:
            return Episode(
                **base,
                episode_id=row["id"],
                five_w=five_w(row["five_w"], binding),
                experience_time=temporal(row["experience_time"]),
                experienced_at=row["experienced_at"],
                context=EpisodeContext(row["context"]),
            )
        if kind is RecordKind.FACT:
            return Fact(
                **base,
                fact_id=row["id"],
                five_w=five_w(row["five_w"], binding),
                target_time=temporal(row["target_time"]),
            )
        evidence = []
        for identifier, version in db.execute(
            (
                "SELECT episode,episode_version FROM memory_semantic_episodes "
                "WHERE binding=%s AND semantic=%s AND semantic_version=%s ORDER "
                "BY seq"
            ),
            (key(binding), row["id"], row["version"]),
        ):
            r = RecordRef(
                kind=RecordKind.EPISODE, record_id=identifier, version=version, binding=binding
            )
            evidence.append(
                EpisodeEvidence(
                    reference=r,
                    sources=frozenset(c.source.reference for c in self._citations(db, binding, r)),
                )
            )
        return Semantic(
            **base,
            semantic_id=row["id"],
            formation_type=FormationType(row["formation_type"]),
            proposition=Proposition(**row["proposition"]),
            applicability=temporal(row["applicability"]),
            episode_evidence=tuple(evidence),
        )

    def get(
        self, binding: Binding, kind: RecordKind, record_id: str, *, version: int | None = None
    ) -> MemoryRecord | None:
        with self.database.transaction(binding) as db:
            row = self._row(db, binding, kind, record_id, version)
            return (
                None
                if row is None or row["state"] != "active"
                else self._decode(db, binding, kind, row)
            )

    def list(self, binding: Binding, kind: RecordKind) -> tuple[MemoryRecord, ...]:
        table = "memory_facts" if kind is RecordKind.FACT else TABLES[kind]
        with self.database.transaction(binding) as db:
            ids = db.execute(
                sql.SQL(
                    "SELECT id FROM {} WHERE binding=%s AND state='active' ORDER BY seq"
                ).format(sql.Identifier(table)),
                (key(binding),),
            ).fetchall()
            result = []
            for (identifier,) in ids:
                row = self._row(db, binding, kind, identifier)
                if row is not None and row["state"] == "active":
                    result.append(self._decode(db, binding, kind, row))
            return tuple(result)

    def head(self, binding: Binding, kind: RecordKind, record_id: str) -> RecordHead | None:
        with self.database.transaction(binding) as db:
            row = self._row(db, binding, kind, record_id)
            return (
                None
                if row is None
                else RecordHead(
                    reference=RecordRef(
                        kind=kind, record_id=record_id, version=row["version"], binding=binding
                    ),
                    state=RecordState(row["state"]),
                )
            )

    def affected(self, binding: Binding, event_id: str) -> tuple[RecordRef, ...]:
        with self.database.transaction(binding) as db:
            return tuple(
                RecordRef(
                    kind=RecordKind(kind), record_id=identifier, version=version, binding=binding
                )
                for kind, identifier, version in db.execute(
                    (
                        "SELECT record_kind,record_id,version FROM memory_event_records "
                        "WHERE binding=%s AND event=%s ORDER BY seq"
                    ),
                    (key(binding), event_id),
                )
            )

    def retrievable(self, binding: Binding) -> tuple[RetrievalCandidate, ...]:
        from .postgres_record_retrieval import snapshot

        with self.database.transaction(binding) as db:
            try:
                return snapshot(db, binding)
            except (ValueError, TypeError, KeyError):
                raise rejected("memory_source_invalid") from None

    def current(self, binding: Binding, values: tuple[RetrievalCandidate, ...]) -> bool:
        if not values:
            return True
        try:
            # Frozen candidates are hashable; set lookup avoids quadratic deep equality.
            current = frozenset(self.retrievable(binding))
            return all(v.record.binding == binding and v in current for v in values)
        except CoreError:
            return False

"""Batched canonical retrieval snapshot; no legacy memories or per-record queries."""

import json
from typing import Any

from psycopg import Connection, sql

from .history import Binding, SourceReference
from .memory_confirmation import blocked, decode
from .memory_contracts import SourceVersion
from .memory_record_store import MemoryRecord, RetrievalCandidate
from .memory_records import (
    Citation,
    Episode,
    EpisodeContext,
    EpisodeEvidence,
    EpisodeFactLink,
    Fact,
    FormationType,
    Proposition,
    RecordKind,
    RecordRef,
    RecordState,
    Semantic,
    Speaker,
    dependency_invalidated,
)
from .postgres_db import key
from .postgres_record_codec import five_w, record_citations, temporal
from .postgres_record_schema import COLUMNS


def snapshot(db: Connection[tuple[Any, ...]], binding: Binding) -> tuple[RetrievalCandidate, ...]:
    """One transaction/scope lock; statement count is independent of record count."""
    rows: dict[RecordKind, list[dict[str, Any]]] = {}
    for kind, table in (
        (RecordKind.EPISODE, "memory_episodes"),
        (RecordKind.FACT, "memory_fact_versions"),
        (RecordKind.SEMANTIC, "memory_semantics"),
        (RecordKind.EPISODE_FACT_LINK, "memory_episode_fact_links"),
    ):
        query: sql.SQL | sql.Composed = sql.SQL(
            "SELECT r.* FROM {table} r WHERE r.binding=%s AND r.state='active' ORDER BY r.seq"
        ).format(table=sql.Identifier(table))
        if kind is RecordKind.FACT:
            query = sql.SQL(
                "SELECT r.* FROM memory_fact_versions r JOIN memory_facts h ON "
                "h.binding=r.binding AND h.id=r.id AND h.version=r.version WHERE r.binding=%s "
                "AND r.state='active' AND h.state='active' ORDER BY r.seq"
            )
        rows[kind] = [
            dict(zip(COLUMNS[table], row, strict=True))
            for row in db.execute(query, (key(binding),))
        ]
    citations: dict[RecordRef, list[Citation]] = {}
    direct: dict[RecordRef, list[Citation]] = {}
    for k, identifier, version, c, r, p, e, speaker, role, start, end in db.execute(
        "SELECT "
        "record_kind,record_id,version,conversation,revision,position,epoch,speaker,"
        "citation_role,start_offset,end_offset FROM memory_record_citations WHERE "
        "binding=%s ORDER BY seq",
        (key(binding),),
    ):
        reference = RecordRef(
            kind=RecordKind(k), record_id=identifier, version=version, binding=binding
        )
        # Reason citations are checked via decoded FiveW too, but they are kept
        # here for complete batch source addressing before any eligibility check.
        citation = Citation(
            binding, SourceVersion(SourceReference(c, r, p), e), Speaker(speaker), start, end
        )
        citations.setdefault(reference, []).append(citation)
        if role == "record":
            direct.setdefault(reference, []).append(citation)
    evidence: dict[tuple[str, int], list[RecordRef]] = {}
    for s, sv, e, ev in db.execute(
        "SELECT semantic,semantic_version,episode,episode_version FROM "
        "memory_semantic_episodes WHERE binding=%s ORDER BY seq",
        (key(binding),),
    ):
        evidence.setdefault((s, sv), []).append(
            RecordRef(kind=RecordKind.EPISODE, record_id=e, version=ev, binding=binding)
        )
    records: dict[RecordRef, MemoryRecord] = {}
    for kind in (
        RecordKind.EPISODE,
        RecordKind.FACT,
        RecordKind.SEMANTIC,
        RecordKind.EPISODE_FACT_LINK,
    ):
        for row in rows[kind]:
            reference = RecordRef(
                kind=kind, record_id=row["id"], version=row["version"], binding=binding
            )
            base = {k: row[k] for k in ("version", "created_at")}
            base.update(binding=binding, state=RecordState.ACTIVE)
            value: MemoryRecord
            if kind is RecordKind.EPISODE_FACT_LINK:
                value = EpisodeFactLink(
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
            else:
                # Preserve only the record-role citations in the model/source
                # value; explicit reason citations retain their own source role.
                cs = tuple(direct.get(reference, ()))
                base.update(
                    normalized_text=row["normalized_text"],
                    last_user_mentioned_at=row["last_user_mentioned_at"],
                    citations=cs,
                )
                if kind is RecordKind.EPISODE:
                    value = Episode(
                        **base,
                        episode_id=row["id"],
                        five_w=five_w(row["five_w"], binding),
                        experience_time=temporal(row["experience_time"]),
                        experienced_at=row["experienced_at"],
                        context=EpisodeContext(row["context"]),
                    )
                elif kind is RecordKind.FACT:
                    value = Fact(
                        **base,
                        fact_id=row["id"],
                        five_w=five_w(row["five_w"], binding),
                        target_time=temporal(row["target_time"]),
                    )
                else:
                    if any(
                        not direct.get(r) for r in evidence.get((row["id"], row["version"]), ())
                    ):
                        continue
                    value = Semantic(
                        **base,
                        semantic_id=row["id"],
                        formation_type=FormationType(row["formation_type"]),
                        proposition=Proposition(**row["proposition"]),
                        applicability=temporal(row["applicability"]),
                        episode_evidence=tuple(
                            EpisodeEvidence(
                                reference=r,
                                sources=frozenset(c.source.reference for c in direct.get(r, ())),
                            )
                            for r in evidence.get((row["id"], row["version"]), ())
                        ),
                    )
            records[reference] = value
    # Restrict history reads to exact conversation/revision pairs addressed by
    # active records; a pair can own any number of positions/citation ranges.
    all_citations = tuple(
        dict.fromkeys(
            c
            for reference, record in records.items()
            for c in (*record_citations(record), *citations.get(reference, ()))
        )
    )
    pairs = tuple(
        dict.fromkeys(
            (c.source.reference.conversation_id, c.source.reference.turn_revision)
            for c in all_citations
        )
    )
    source_rows = {}
    if pairs:
        for cid, revision, cp, epoch, tp, excluded, messages, confirmation in db.execute(
            "SELECT "
            "c.id,t.revision,c.private_mode,c.memory_epoch,t.private_mode,"
            "t.memory_excluded,t.messages,t.memory_confirmation FROM unnest(%s::text[],"
            "%s::integer[]) AS wanted(conversation,revision) JOIN conversations c ON "
            "c.binding=%s AND c.id=wanted.conversation JOIN turns t ON "
            "t.binding=c.binding AND t.conversation=c.id AND t.revision=wanted.revision",
            ([c for c, _ in pairs], [r for _, r in pairs], key(binding)),
        ):
            source_rows[cid, revision] = (
                cp,
                epoch,
                tp,
                json.loads(excluded),
                json.loads(messages),
                decode(confirmation),
            )

    def source_valid(c: Citation) -> bool:
        s = c.source.reference
        row = source_rows.get((s.conversation_id, s.turn_revision))
        if row is None:
            return False
        cp, epoch, tp, excluded, messages, confirmation = row
        if (
            c.binding != binding
            or cp
            or tp
            or epoch != c.source.epoch
            or s.message_index in excluded
            or blocked(confirmation, s.message_index)
            or not 0 <= s.message_index < len(messages)
        ):
            return False
        message = messages[s.message_index]
        text = message.get("content")
        return (
            message.get("role") == c.speaker.value
            and isinstance(text, str)
            and 0 <= c.start < c.end <= len(text)
        )

    eligible: dict[RecordRef, MemoryRecord] = {
        r: v
        for r, v in records.items()
        if all(source_valid(c) for c in (*record_citations(v), *citations.get(r, ())))
    }
    # Index links once by Episode, keeping eligible order for each Episode's links.
    links_by_episode: dict[RecordRef, list[EpisodeFactLink]] = {}
    for value in eligible.values():
        if isinstance(value, EpisodeFactLink):
            links_by_episode.setdefault(value.episode, []).append(value)
    result = []
    for r, value in eligible.items():
        if isinstance(value, Episode):
            links = tuple(
                link
                for link in links_by_episode.get(r, ())
                if not dependency_invalidated(link.fact, eligible.get(link.fact))
            )
            facts = tuple(dict.fromkeys(eligible[link.fact] for link in links))
            assert all(isinstance(f, Fact) for f in facts)
            result.append(
                RetrievalCandidate(value, tuple(f for f in facts if isinstance(f, Fact)), links)
            )
        elif isinstance(value, Semantic):
            dependencies = tuple(eligible.get(ev.reference) for ev in value.episode_evidence)
            if any(
                dependency_invalidated(ev.reference, e)
                for ev, e in zip(value.episode_evidence, dependencies, strict=True)
            ):
                continue
            if any(
                ev.sources != frozenset(c.source.reference for c in e.citations)
                for ev, e in zip(value.episode_evidence, dependencies, strict=True)
                if isinstance(e, Episode)
            ):
                continue
            result.append(
                RetrievalCandidate(
                    value, evidence=tuple(e for e in dependencies if isinstance(e, Episode))
                )
            )
    return tuple(result)

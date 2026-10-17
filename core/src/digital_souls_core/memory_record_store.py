"""Storage-independent canonical memory port. Admission/formation belong to callers."""

from dataclasses import dataclass
from typing import Protocol

from .history import Binding
from .memory_records import (
    Citation,
    Episode,
    EpisodeFactLink,
    Fact,
    RecordHead,
    RecordKind,
    RecordRef,
    Semantic,
)

type MemoryRecord = Episode | Fact | EpisodeFactLink | Semantic


@dataclass(frozen=True)
class FactWrite:
    fact: Fact
    expected_version: int | None = None


@dataclass(frozen=True)
class RecordBatch:
    episodes: tuple[Episode, ...] = ()
    facts: tuple[FactWrite, ...] = ()
    links: tuple[EpisodeFactLink, ...] = ()
    semantics: tuple[Semantic, ...] = ()


@dataclass(frozen=True)
class RetrievalCandidate:
    """Canonical candidate and the exact active Fact/link snapshots it carries.

    Evidence Episodes are retained for derived Semantic provenance, not embedded.
    """

    record: Episode | Semantic
    facts: tuple[Fact, ...] = ()
    links: tuple[EpisodeFactLink, ...] = ()
    evidence: tuple[Episode, ...] = ()

    @property
    def identifier(self) -> str:
        return (
            self.record.episode_id if isinstance(self.record, Episode) else self.record.semantic_id
        )

    @property
    def texts(self) -> tuple[str, ...]:
        return (self.record.normalized_text, *(f.normalized_text for f in self.facts))

    @property
    def citations(self) -> tuple[Citation, ...]:
        values: list[Citation] = []
        for record in (self.record, *self.facts, *self.evidence):
            values.extend(record.citations)
            if isinstance(record, (Episode, Fact)) and record.five_w.why is not None:
                values.extend(record.five_w.why.citations)
        return tuple(dict.fromkeys(values))


class MemoryRecordStore(Protocol):
    def retrievable(self, binding: Binding) -> tuple[RetrievalCandidate, ...]:
        """Active current Episode/Semantic, eligible sources and valid linked Facts."""
        ...

    def current(self, binding: Binding, values: tuple[RetrievalCandidate, ...]) -> bool:
        """Recheck all record/link versions, citations and Semantic dependencies."""
        ...

    def register(
        self, binding: Binding, batch: RecordBatch, formation_version: str
    ) -> tuple[RecordRef, ...]:
        """Atomically register active records; matching retry returns original references.

        New identities start at v1. Fact writes require the expected head version
        and exactly its successor. Stale sources, references, IDs and conflicting
        retries fail closed. No operation reactivates a suspended identity.
        """
        ...

    def get(
        self, binding: Binding, kind: RecordKind, record_id: str, *, version: int | None = None
    ) -> MemoryRecord | None: ...

    def list(self, binding: Binding, kind: RecordKind) -> tuple[MemoryRecord, ...]:
        """Active current versions only; get(version=...) can read active Fact history."""
        ...

    def head(self, binding: Binding, kind: RecordKind, record_id: str) -> RecordHead | None:
        """Content-free identity, including suspended records."""
        ...

    def affected(self, binding: Binding, event_id: str) -> tuple[RecordRef, ...]:
        """Content-free reevaluation targets, independent of legacy event consumption."""
        ...

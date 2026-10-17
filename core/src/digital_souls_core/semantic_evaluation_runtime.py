"""Reusable synthetic history/record setup and production retrieval for #113/#114.

No formation, ranking implementation, real classifier or vector cache lives here.
Only freshly allocated schemas are dropped; source/epoch mutations use public ports.
"""

import hashlib
import json
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from psycopg import sql

from .application import CoreError
from .character import Character, CharacterConfig, GuardedContext, Profile
from .contracts import Message
from .history import Binding, ConversationControls, HistoryStore, SourceReference, TurnDeletionInput
from .memory import MemoryContext
from .memory_ranking import EmbeddingSpace, MemoryEmbedding
from .memory_record_store import FactWrite, MemoryRecordStore, RecordBatch, RetrievalCandidate
from .memory_records import Citation, Episode, Fact, Semantic
from .memory_retrieval import MemoryRetrieval
from .postgres_db import PostgresConfig, PostgresDatabase
from .postgres_history import PostgresHistory
from .postgres_memory_records import PostgresMemoryRecords
from .privacy import PrivacyPolicy
from .privacy_classifier import LocalClassifier
from .privacy_scan import POLICY_VERSION
from .prompt_builder import CharacterCard
from .semantic_evaluation_cases import (
    EvaluationCase,
    EvaluationCases,
    EvaluationEpisode,
    EvaluationFact,
    EvaluationSemantic,
    ExcludeMutation,
    FactUpdateMutation,
    Phase,
    SemanticEvaluationData,
    registration_batches,
)


class SyntheticClassifierProvider:
    """Replace only Provider output, retaining LocalClassifier/scanner/policy checks."""

    async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "classification": "NOT_SENSITIVE",
                                "subject": "GENERAL",
                                "category": "NONE",
                                "policy_version": POLICY_VERSION,
                            }
                        ),
                    },
                }
            ],
        }

    async def stream(
        self, profile: Profile, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        raise RuntimeError("Synthetic classifier does not stream")
        yield  # pragma: no cover


def synthetic_policy(
    case: EvaluationCase, *, external: bool = False
) -> tuple[PrivacyPolicy, Profile]:
    profile = Profile(
        profile_id="synthetic-classifier",
        model="openai/gemma4-12b",
        transport="llamacpp_chat",
        api_base="http://127.0.0.1:18080/v1",
        external_send_allowed=True,
        allowed_parameters=frozenset({"max_completion_tokens"}),
    )
    policy = PrivacyPolicy(
        LocalClassifier(SyntheticClassifierProvider(), profile, model_digest="synthetic-v1")
    )
    bindings = {case.binding.to_domain(), *(c.binding.to_domain() for c in case.conversations)}
    policy.configure(
        {
            b: frozenset(
                {"history", "local", "memory", "external"}
                if external
                else {"history", "local", "memory"}
            )
            for b in bindings
        }
    )
    return policy, profile


class FixtureEmbedding:
    def __init__(self, data: SemanticEvaluationData | EvaluationCases) -> None:
        cases = data.cases if isinstance(data, SemanticEvaluationData) else data
        self.space = EmbeddingSpace(
            "synthetic", "semantic-cases-v1", cases.dimensions, "fixture-no-cache"
        )
        self._vectors = {}
        for case in cases.cases:
            self._vectors[case.query] = case.query_vector
            for record in (
                *case.episodes,
                *case.facts,
                *case.semantics,
                *(m.fact for m in case.mutations if isinstance(m, FactUpdateMutation)),
            ):
                self._vectors[record.normalized_text] = record.vector

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._vectors[text] for text in texts)


@dataclass(frozen=True)
class EmbeddingCall:
    texts: tuple[str, ...] = field(repr=False)
    vectors: tuple[tuple[float, ...], ...] = field(repr=False)


class RecordingEmbedding:
    """In-memory input/output trace for independent checks, never a report or cache."""

    def __init__(self, delegate: MemoryEmbedding) -> None:
        self.delegate = delegate
        self.calls: list[EmbeddingCall] = []

    @property
    def space(self) -> EmbeddingSpace:
        return self.delegate.space

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        vectors = await self.delegate.embed(texts)
        self.calls.append(EmbeddingCall(texts, vectors))
        return vectors


class CapturingRetrieval(MemoryRetrieval):
    """Observe super().search so optional-context fallback cannot hide an error."""

    def __init__(
        self, records: MemoryRecordStore, policy: PrivacyPolicy, embedding: MemoryEmbedding
    ) -> None:
        super().__init__(records, policy, embedding=embedding)
        self.results: list[tuple[RetrievalCandidate, ...]] = []
        self.failed = False

    async def search(
        self,
        binding: Binding,
        query: str,
        limit: int | None = None,
        *,
        authorized: Callable[[], bool] = lambda: True,
    ) -> tuple[RetrievalCandidate, ...]:
        try:
            result = await super().search(binding, query, limit, authorized=authorized)
            self.results.append(result)
            return result
        except Exception:
            self.failed = True
            raise


def remap_batch(batch: RecordBatch, ids: dict[str, str]) -> RecordBatch:
    def source(ref: SourceReference) -> SourceReference:
        return replace(ref, conversation_id=ids[ref.conversation_id])

    def citation(c: Citation) -> Citation:
        return replace(c, source=replace(c.source, reference=source(c.source.reference)))

    def record[T: Episode | Fact | Semantic](r: T) -> T:
        r = replace(r, citations=tuple(citation(c) for c in r.citations))
        if isinstance(r, (Episode, Fact)) and r.five_w.why is not None:
            r = replace(
                r,
                five_w=replace(
                    r.five_w,
                    why=replace(
                        r.five_w.why, citations=tuple(citation(c) for c in r.five_w.why.citations)
                    ),
                ),
            )
        if isinstance(r, Semantic):
            r = replace(
                r,
                episode_evidence=tuple(
                    replace(e, sources=frozenset(source(s) for s in e.sources))
                    for e in r.episode_evidence
                ),
            )
        return r

    return RecordBatch(
        episodes=tuple(record(r) for r in batch.episodes),
        facts=tuple(FactWrite(record(w.fact), w.expected_version) for w in batch.facts),
        links=batch.links,
        semantics=tuple(record(r) for r in batch.semantics),
    )


@dataclass
class CaseClock:
    value: datetime = datetime(2026, 10, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


@dataclass
class PreparedCase:
    case: EvaluationCase = field(repr=False)
    history: HistoryStore = field(repr=False)
    records: MemoryRecordStore = field(repr=False)
    conversation_ids: dict[str, str]
    rejected_ids: tuple[str, ...]
    retrieval: CapturingRetrieval = field(repr=False)
    embedding: RecordingEmbedding = field(repr=False)
    character: Character = field(repr=False)

    def mutate(self, phase: Phase) -> None:
        """after_answer is exposed for #114; retrieval evaluator stops at dispatch."""
        conversations = {c.id: c for c in self.case.conversations}
        for mutation in self.case.mutations:
            if mutation.phase != phase or isinstance(mutation, ExcludeMutation):
                continue
            if isinstance(mutation, FactUpdateMutation):
                self.records.register(
                    mutation.fact.binding.to_domain(),
                    remap_batch(mutation.batch(), self.conversation_ids),
                    "semantic-evaluation-v1",
                )
                continue
            cid = self.conversation_ids[mutation.conversation_id]
            binding = conversations[mutation.conversation_id].binding.to_domain()
            if mutation.op == "delete_conversation":
                self.history.delete(binding, cid)
            else:
                revision = self.history.read(binding, cid).revision
                if mutation.op == "set_private":
                    self.history.controls(
                        binding,
                        cid,
                        ConversationControls(
                            expected_revision=revision, private_mode=mutation.private_mode
                        ),
                    )
                else:
                    self.history.delete_turns(
                        binding,
                        cid,
                        TurnDeletionInput(
                            expected_revision=revision,
                            turn_revision=mutation.turn_revision,
                            scope="selected",
                        ),
                    )

    async def search_context(self) -> tuple[tuple[RetrievalCandidate, ...], GuardedContext]:
        self.retrieval.results.clear()
        self.retrieval.failed = False
        self.embedding.calls.clear()
        binding = self.case.binding.to_domain()
        context = await MemoryContext(self.retrieval).context(
            self.character, binding.scope, self.case.query, authorized=lambda: True
        )
        if self.retrieval.failed or len(self.retrieval.results) != 1:
            raise ValueError("Evaluation context retrieval failed")
        return self.retrieval.results[0], context


def prepare_case(
    case: EvaluationCase,
    history: HistoryStore,
    records: MemoryRecordStore,
    clock: CaseClock,
    embedding: MemoryEmbedding,
    *,
    external: bool = False,
) -> PreparedCase:
    """Create/append -> remap/register -> rejection probes -> before_search mutations."""
    ids: dict[str, str] = {}
    for conv in case.conversations:
        binding = conv.binding.to_domain()
        cid = history.create(binding).conversation_id
        ids[conv.id] = cid
        for revision in sorted({m.turn_revision for m in conv.messages}):
            messages = tuple(m for m in conv.messages if m.turn_revision == revision)
            if messages[0].stated_at is None:
                raise ValueError("Evaluation requires a trusted turn clock")
            clock.value = messages[0].stated_at
            exclusions = tuple(
                m.message_index
                for m in case.mutations
                if isinstance(m, ExcludeMutation)
                and m.conversation_id == conv.id
                and m.turn_revision == revision
            )
            history.append(
                binding,
                cid,
                f"turn-{revision}",
                f"synthetic-{revision}",
                revision - 1,
                tuple(Message(role=m.role, content=m.content) for m in messages),
                "stop",
                memory_excluded_indices=exclusions,
            )
    batches = registration_batches(case)
    for binding, batch in batches:
        records.register(binding, remap_batch(batch, ids), "semantic-evaluation-v1")
    included = (
        {r.episode_id for _, batch in batches for r in batch.episodes}
        | {w.fact.fact_id for _, batch in batches for w in batch.facts}
        | {r.semantic_id for _, batch in batches for r in batch.semantics}
    )
    rejected: list[str] = []
    probes: list[EvaluationEpisode | EvaluationFact | EvaluationSemantic] = [
        *case.episodes,
        *case.facts,
        *case.semantics,
    ]
    for r in probes:
        if r.id in included:
            continue
        domain = r.to_domain()
        batch = (
            RecordBatch(episodes=(domain,))
            if isinstance(domain, Episode)
            else RecordBatch(facts=(FactWrite(domain),))
            if isinstance(domain, Fact)
            else RecordBatch(semantics=(domain,))
        )
        try:
            records.register(
                r.binding.to_domain(), remap_batch(batch, ids), "semantic-evaluation-probe-v1"
            )
        except CoreError as error:
            if error.code != "memory_source_invalid":
                raise
            rejected.append(r.id)
        else:
            raise ValueError("Excluded source registration was accepted")
    policy, profile = synthetic_policy(case, external=external)
    recording = RecordingEmbedding(embedding)
    runtime = PreparedCase(
        case,
        history,
        records,
        ids,
        tuple(rejected),
        CapturingRetrieval(records, policy, recording),
        recording,
        Character(
            CharacterConfig(
                character_id=case.binding.character_id,
                alias="synthetic",
                config_version="synthetic-v1",
                card_path="synthetic.json",
                profile=profile,
            ),
            CharacterCard(system_prompt="Synthetic evaluation"),
            card_sha256=hashlib.sha256(b"Synthetic evaluation").hexdigest(),
        ),
    )
    runtime.mutate("before_search")
    return runtime


@contextmanager
def isolated_case(
    config: PostgresConfig,
    case: EvaluationCase,
    embedding: MemoryEmbedding,
    *,
    external: bool = False,
) -> Iterator[PreparedCase]:
    """Allocate a fresh schema per case AND run, including overlapping record IDs."""
    config = config.model_copy(update={"schema_name": "dsc_eval_" + uuid4().hex})
    database = PostgresDatabase(config)
    clock = CaseClock()
    try:
        history = PostgresHistory(database, clock=clock)
        records = PostgresMemoryRecords(database)
        yield prepare_case(case, history, records, clock, embedding, external=external)
    finally:
        with database._connect() as db:
            db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(config.schema_name)
                )
            )

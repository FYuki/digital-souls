"""Strict, body-safe synthetic cases and gold; pure conversion, never a harness.

Parse JSON through the public functions so validation/IO errors cannot disclose
source text. Case data and gold remain distinct types and files.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from unicodedata import normalize
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .character import AccessScope
from .history import Binding, SourceReference
from .memory_contracts import SourceVersion
from .memory_record_store import FactWrite, RecordBatch
from .memory_records import (
    Citation,
    Episode,
    EpisodeContext,
    EpisodeEvidence,
    EpisodeFactLink,
    ExplicitReason,
    Fact,
    FiveW,
    FormationType,
    PartialDateTime,
    Proposition,
    RecordKind,
    RecordRef,
    RecordState,
    Semantic,
    Speaker,
    TemporalValue,
    TimePrecision,
)

type Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,95}$")]
type Text = Annotated[str, Field(min_length=1)]
type FactGroup = Annotated[tuple[Text, ...], Field(min_length=1)]
type Positive = Annotated[int, Field(ge=1)]
type Nonnegative = Annotated[int, Field(ge=0)]
type Component = Annotated[float, Field(allow_inf_nan=False)]
type Vector = tuple[Component, ...]
type Phase = Literal["before_search", "after_search", "after_answer"]
type Category = Literal[
    "synonym",
    "paraphrase",
    "cross_language",
    "unrelated",
    "negation",
    "update",
    "multisource",
    "multisource_revocation",
    "private",
    "excluded",
    "deleted_source",
    "deleted_memory",
    "binding_character",
    "binding_subject",
    "binding_client",
    "long_text",
    "after_search",
    "after_answer",
    "epoch_change",
    "fact_attachment",
    "fact_version",
    "fact_revocation",
    "semantic_direct",
    "semantic_derived",
    "equivalent_order",
    "threshold",
]


class EvaluationDataError(ValueError):
    """A content-free boundary error; never exposes Pydantic's input payload."""


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    def __repr__(self) -> str:
        # Nested schema objects can carry text in many fields, including FiveW.
        return f"{type(self).__name__}()"

    def __str__(self) -> str:
        return repr(self)


class EvaluationBinding(_Model):
    subject: Identifier
    client: Identifier
    audience: Literal["local-private"]
    character_id: Identifier

    def to_domain(self) -> Binding:
        return Binding(AccessScope(self.subject, self.client, self.audience), self.character_id)


class SourceAddress(_Model):
    conversation_id: Identifier
    turn_revision: Positive
    message_index: Nonnegative

    def to_domain(self) -> SourceReference:
        return SourceReference(self.conversation_id, self.turn_revision, self.message_index)


class EvaluationCitation(SourceAddress):
    binding: EvaluationBinding
    epoch: Nonnegative
    speaker: Literal["user", "assistant", "system", "developer", "tool"]
    start: Nonnegative
    end: Positive

    def citation(self) -> Citation:
        return Citation(
            self.binding.to_domain(),
            SourceVersion(self.to_domain(), self.epoch),
            Speaker(self.speaker),
            self.start,
            self.end,
        )


class EvaluationMessage(_Model):
    role: Literal["user", "assistant"]
    content: Text = Field(repr=False)
    turn_revision: Positive
    message_index: Nonnegative
    stated_at: datetime | None


class EvaluationConversation(_Model):
    id: Identifier
    binding: EvaluationBinding
    messages: tuple[EvaluationMessage, ...] = Field(min_length=2, repr=False)


class EvaluationPartialTime(_Model):
    precision: Literal["year", "month", "day", "hour", "minute", "second"]
    year: int
    month: int | None = None
    day: int | None = None
    hour: int | None = None
    minute: int | None = None
    second: int | None = None

    def to_domain(self) -> PartialDateTime:
        return PartialDateTime(
            **{**self.model_dump(exclude={"precision"}), "precision": TimePrecision(self.precision)}
        )


class EvaluationTime(_Model):
    start: EvaluationPartialTime | None = None
    end: EvaluationPartialTime | None = None
    timezone: Text | None = None

    def to_domain(self) -> TemporalValue:
        if self.timezone is not None:
            ZoneInfo(self.timezone)
        return TemporalValue(
            start=self.start.to_domain() if self.start else None,
            end=self.end.to_domain() if self.end else None,
            timezone=self.timezone,
        )


class EvaluationReason(_Model):
    text: Text = Field(repr=False)
    citations: tuple[EvaluationCitation, ...] = Field(min_length=1)


class EvaluationFiveW(_Model):
    predicate: Text = Field(repr=False)
    object: Text | None = Field(default=None, repr=False)
    who: Text | None = Field(default=None, repr=False)
    when: EvaluationTime | None = None
    where: Text | None = Field(default=None, repr=False)
    why: EvaluationReason | None = Field(default=None, repr=False)

    def to_domain(self) -> FiveW:
        return FiveW(
            predicate=self.predicate,
            object=self.object,
            who=self.who,
            when=self.when.to_domain() if self.when else None,
            where=self.where,
            why=ExplicitReason(self.why.text, tuple(c.citation() for c in self.why.citations))
            if self.why
            else None,
        )


class _Record(_Model):
    id: Identifier
    version: Positive
    binding: EvaluationBinding
    created_at: datetime
    state: Literal["active"]


class _ContentRecord(_Record):
    normalized_text: Text = Field(repr=False)
    last_user_mentioned_at: datetime | None
    vector: Vector = Field(repr=False)
    citations: tuple[EvaluationCitation, ...]


class EvaluationEpisode(_ContentRecord):
    five_w: EvaluationFiveW = Field(repr=False)
    experience_time: EvaluationTime
    experienced_at: datetime | None
    context: Literal["actual", "hypothetical", "fiction"]

    def to_domain(self) -> Episode:
        return Episode(
            episode_id=self.id,
            version=self.version,
            binding=self.binding.to_domain(),
            normalized_text=self.normalized_text,
            created_at=self.created_at,
            last_user_mentioned_at=self.last_user_mentioned_at,
            state=RecordState.ACTIVE,
            citations=tuple(c.citation() for c in self.citations),
            five_w=self.five_w.to_domain(),
            experience_time=self.experience_time.to_domain(),
            experienced_at=self.experienced_at,
            context=EpisodeContext(self.context),
        )


class EvaluationFact(_ContentRecord):
    five_w: EvaluationFiveW = Field(repr=False)
    target_time: EvaluationTime

    def to_domain(self) -> Fact:
        return Fact(
            fact_id=self.id,
            version=self.version,
            binding=self.binding.to_domain(),
            normalized_text=self.normalized_text,
            created_at=self.created_at,
            last_user_mentioned_at=self.last_user_mentioned_at,
            state=RecordState.ACTIVE,
            citations=tuple(c.citation() for c in self.citations),
            five_w=self.five_w.to_domain(),
            target_time=self.target_time.to_domain(),
        )


class EvaluationLink(_Record):
    episode_id: Identifier
    episode_version: Positive
    fact_id: Identifier
    fact_version: Positive

    def to_domain(self) -> EpisodeFactLink:
        binding = self.binding.to_domain()
        return EpisodeFactLink(
            link_id=self.id,
            version=self.version,
            binding=binding,
            created_at=self.created_at,
            state=RecordState.ACTIVE,
            episode=RecordRef(
                kind=RecordKind.EPISODE,
                record_id=self.episode_id,
                version=self.episode_version,
                binding=binding,
            ),
            fact=RecordRef(
                kind=RecordKind.FACT,
                record_id=self.fact_id,
                version=self.fact_version,
                binding=binding,
            ),
        )


class EvaluationEvidence(_Model):
    episode_id: Identifier
    version: Positive
    sources: tuple[SourceAddress, ...] = Field(min_length=1)


class EvaluationProposition(_Model):
    subject: Text = Field(repr=False)
    attribute: Text = Field(repr=False)
    value: Text = Field(repr=False)


class EvaluationSemantic(_ContentRecord):
    formation_type: Literal["direct_extraction", "experience_derived"]
    proposition: EvaluationProposition = Field(repr=False)
    applicability: EvaluationTime
    episode_evidence: tuple[EvaluationEvidence, ...]

    def to_domain(self) -> Semantic:
        return Semantic(
            semantic_id=self.id,
            version=self.version,
            binding=self.binding.to_domain(),
            normalized_text=self.normalized_text,
            created_at=self.created_at,
            last_user_mentioned_at=self.last_user_mentioned_at,
            state=RecordState.ACTIVE,
            citations=tuple(c.citation() for c in self.citations),
            formation_type=FormationType(self.formation_type),
            proposition=Proposition(**self.proposition.model_dump()),
            applicability=self.applicability.to_domain(),
            episode_evidence=tuple(
                EpisodeEvidence(
                    reference=RecordRef(
                        kind=RecordKind.EPISODE,
                        record_id=e.episode_id,
                        version=e.version,
                        binding=self.binding.to_domain(),
                    ),
                    sources=frozenset(s.to_domain() for s in e.sources),
                )
                for e in self.episode_evidence
            ),
        )


class _Mutation(_Model):
    phase: Phase


class PrivateMutation(_Mutation):
    op: Literal["set_private"]
    conversation_id: Identifier
    private_mode: bool


class DeleteConversationMutation(_Mutation):
    op: Literal["delete_conversation"]
    conversation_id: Identifier


class DeleteTurnMutation(_Mutation):
    op: Literal["delete_turn"]
    conversation_id: Identifier
    turn_revision: Positive


class ExcludeMutation(_Mutation):
    """Applied during history append, before registration, never a retroactive edit."""

    phase: Literal["before_search"]
    op: Literal["exclude_on_append"]
    conversation_id: Identifier
    turn_revision: Positive
    message_index: Nonnegative


class FactUpdateMutation(_Mutation):
    op: Literal["update_fact"]
    expected_version: Positive
    fact: EvaluationFact = Field(repr=False)
    links: tuple[EvaluationLink, ...]

    def batch(self) -> RecordBatch:
        return RecordBatch(
            facts=(FactWrite(self.fact.to_domain(), self.expected_version),),
            links=tuple(link.to_domain() for link in self.links),
        )


type Mutation = Annotated[
    PrivateMutation
    | DeleteConversationMutation
    | DeleteTurnMutation
    | ExcludeMutation
    | FactUpdateMutation,
    Field(discriminator="op"),
]


class EvaluationCase(_Model):
    id: Identifier
    legacy_id: Identifier | None
    query: Annotated[str, Field(min_length=1, max_length=256, repr=False)]
    query_vector: Vector = Field(repr=False)
    binding: EvaluationBinding
    conversations: tuple[EvaluationConversation, ...] = Field(min_length=1, repr=False)
    episodes: tuple[EvaluationEpisode, ...] = Field(repr=False)
    facts: tuple[EvaluationFact, ...] = Field(repr=False)
    links: tuple[EvaluationLink, ...]
    semantics: tuple[EvaluationSemantic, ...] = Field(repr=False)
    mutations: tuple[Mutation, ...] = Field(repr=False)


class EvaluationCases(_Model):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    dataset_type: Literal["synthetic"]
    dimensions: Annotated[int, Field(ge=1, le=4096)]
    cases: tuple[EvaluationCase, ...] = Field(min_length=1, repr=False)


class DispatchExpectation(_Model):
    valid: bool
    memory_ids: tuple[Identifier, ...]


def normalize_fact_text(text: str) -> str:
    """Shared gold/answer normalization: NFKC followed by Unicode casefold."""
    return normalize("NFKC", text).casefold()


class AnswerExpectation(_Model):
    behavior: Literal["grounded", "no_memory", "blocked"]
    required_facts: tuple[FactGroup, ...] = Field(repr=False)
    forbidden_facts: tuple[FactGroup, ...] = Field(repr=False)
    discarded: bool

    @field_validator("required_facts", "forbidden_facts")
    @classmethod
    def validate_groups(cls, groups: tuple[FactGroup, ...]) -> tuple[FactGroup, ...]:
        seen: set[str] = set()
        for group in groups:
            for text in group:
                normalized = normalize_fact_text(text)
                if not normalized.strip() or normalized in seen:
                    raise ValueError("Invalid answer fact groups")
                seen.add(normalized)
        return groups

    @model_validator(mode="after")
    def validate_fact_overlap(self) -> "AnswerExpectation":
        required = {normalize_fact_text(t) for g in self.required_facts for t in g}
        forbidden = {normalize_fact_text(t) for g in self.forbidden_facts for t in g}
        if required & forbidden:
            raise ValueError("Overlapping answer facts")
        return self

    def matches_facts(self, answer: str) -> bool:
        """Pure lexical predicate only; does not check behavior, guards or discard."""
        normalized = normalize_fact_text(answer)
        return all(
            any(normalize_fact_text(t) in normalized for t in group)
            for group in self.required_facts
        ) and not any(
            normalize_fact_text(t) in normalized for group in self.forbidden_facts for t in group
        )


class CaseExpectation(_Model):
    id: Identifier
    category: Category
    relevant_ids: tuple[Identifier, ...]
    forbidden_ids: tuple[Identifier, ...]
    no_match: bool
    expected_order: tuple[Identifier, ...] | None
    # Fact IDs are checked inside attachments/context, not as search candidate IDs.
    required_fact_ids: tuple[Identifier, ...]
    forbidden_fact_ids: tuple[Identifier, ...]
    dispatch: DispatchExpectation
    answer: AnswerExpectation


def expected_top_one(gold: CaseExpectation) -> str | None:
    """Derive the intended first memory from fixed gold, without changing it."""
    ids = gold.expected_order or gold.relevant_ids
    return ids[0] if ids else None


class EvaluationExpectations(_Model):
    schema_version: Annotated[int, Field(ge=2, le=2)]
    cases: tuple[CaseExpectation, ...] = Field(min_length=1, repr=False)


@dataclass(frozen=True)
class SemanticEvaluationData:
    cases: EvaluationCases = field(repr=False)
    expectations: EvaluationExpectations = field(repr=False)


def _unique(values: tuple[str, ...]) -> set[str]:
    result = set(values)
    if len(result) != len(values):
        raise ValueError
    return result


def _validate_case(case: EvaluationCase) -> None:
    conversations = {c.id: c for c in case.conversations}
    _unique(tuple(c.id for c in case.conversations))
    sources: dict[SourceReference, tuple[EvaluationConversation, EvaluationMessage]] = {}
    for conv in case.conversations:
        # Each synthetic turn is an append-compatible user/assistant pair. Actual
        # generated conversation UUIDs are substituted by the later harness.
        revisions = sorted({m.turn_revision for m in conv.messages})
        if revisions != list(range(1, len(revisions) + 1)):
            raise ValueError
        for revision in revisions:
            messages = [m for m in conv.messages if m.turn_revision == revision]
            if [m.message_index for m in messages] != [0, 1] or [m.role for m in messages] != [
                "user",
                "assistant",
            ]:
                raise ValueError
            if messages[0].stated_at != messages[1].stated_at:
                raise ValueError
        for message in conv.messages:
            if message.stated_at is not None and message.stated_at.utcoffset() is None:
                raise ValueError
            ref = SourceReference(conv.id, message.turn_revision, message.message_index)
            if ref in sources:
                raise ValueError
            sources[ref] = conv, message

    def citations(values: tuple[EvaluationCitation, ...], binding: EvaluationBinding) -> None:
        for citation in values:
            conv, message = sources[citation.to_domain()]
            if (
                citation.binding != binding
                or conv.binding != binding
                or citation.epoch != 0
                or citation.speaker != message.role
                or not 0 <= citation.start < citation.end <= len(message.content)
            ):
                raise ValueError
            citation.citation()

    records: tuple[
        EvaluationEpisode | EvaluationFact | EvaluationLink | EvaluationSemantic, ...
    ] = (*case.episodes, *case.facts, *case.links, *case.semantics)
    _unique(tuple(r.id for r in records))
    if not records:
        raise ValueError
    episodes = {r.id: r for r in case.episodes}
    facts = {r.id: r for r in case.facts}
    for record in records:
        if record.version != 1 or record.created_at.utcoffset() is None:
            raise ValueError
        record.to_domain()
        if isinstance(record, _ContentRecord):
            citations(record.citations, record.binding)
        if isinstance(record, (EvaluationEpisode, EvaluationFact)) and record.five_w.why:
            citations(record.five_w.why.citations, record.binding)
        if isinstance(record, EvaluationLink):
            episode, fact = episodes[record.episode_id], facts[record.fact_id]
            if (
                record.binding != episode.binding
                or record.binding != fact.binding
                or record.episode_version != episode.version
                or record.fact_version != fact.version
            ):
                raise ValueError
        if isinstance(record, EvaluationSemantic):
            _unique(tuple(e.episode_id for e in record.episode_evidence))
            for evidence in record.episode_evidence:
                episode = episodes[evidence.episode_id]
                actual = {c.to_domain() for c in episode.citations}
                specified = {s.to_domain() for s in evidence.sources}
                if (
                    episode.binding != record.binding
                    or episode.version != evidence.version
                    or actual != specified
                    or len(specified) != len(evidence.sources)
                ):
                    raise ValueError
    known_ids = {r.id for r in records}
    excluded: set[SourceReference] = set()
    phases = {"before_search": 0, "after_search": 1, "after_answer": 2}
    phase_order = [phases[m.phase] for m in case.mutations]
    if phase_order != sorted(phase_order):
        raise ValueError
    for mutation in case.mutations:
        if isinstance(mutation, FactUpdateMutation):
            fact = mutation.fact
            previous = facts[fact.id]
            if (
                fact.version != mutation.expected_version + 1
                or previous.version != mutation.expected_version
                or fact.binding != previous.binding
            ):
                raise ValueError
            fact.to_domain()
            citations(fact.citations, fact.binding)
            if fact.five_w.why:
                citations(fact.five_w.why.citations, fact.binding)
            for link in mutation.links:
                episode = episodes[link.episode_id]
                if (
                    link.id in known_ids
                    or link.version != 1
                    or link.fact_id != fact.id
                    or link.fact_version != fact.version
                    or link.episode_version != episode.version
                    or link.binding != fact.binding
                    or link.binding != episode.binding
                ):
                    raise ValueError
                link.to_domain()
                known_ids.add(link.id)
            facts[fact.id] = fact
            mutation.batch()
        else:
            conv = conversations[mutation.conversation_id]
            if isinstance(mutation, (DeleteTurnMutation, ExcludeMutation)):
                if mutation.turn_revision not in {m.turn_revision for m in conv.messages}:
                    raise ValueError
            if isinstance(mutation, ExcludeMutation):
                ref = SourceReference(conv.id, mutation.turn_revision, mutation.message_index)
                if sources[ref][1].role != "user":
                    raise ValueError
                excluded.add(ref)
    # Only intentionally inadmissible excluded-source records can be withheld.
    for content_record in (*case.episodes, *case.facts, *case.semantics):
        is_excluded = any(c.to_domain() in excluded for c in content_record.citations)
        if is_excluded and any(
            link.episode_id == content_record.id or link.fact_id == content_record.id
            for link in case.links
        ):
            raise ValueError
    batches = registration_batches(case)
    admitted_episodes = {e.episode_id for _, batch in batches for e in batch.episodes}
    for _, batch in batches:
        if any(
            ev.reference.record_id not in admitted_episodes
            for semantic in batch.semantics
            for ev in semantic.episode_evidence
        ):
            raise ValueError


def registration_batches(case: EvaluationCase) -> tuple[tuple[Binding, RecordBatch], ...]:
    """Initial ACTIVE v1 records grouped by Binding, excluding rejection probes.

    All IDs/citation addresses are logical fixture names. A DB harness must remap
    conversation IDs to create() results in history, citations and evidence alike.
    Rejection probes remain available on the case and convert with to_domain().
    """
    excluded = {
        SourceReference(m.conversation_id, m.turn_revision, m.message_index)
        for m in case.mutations
        if isinstance(m, ExcludeMutation)
    }
    withheld = {
        r.id
        for r in (*case.episodes, *case.facts, *case.semantics)
        if any(c.to_domain() in excluded for c in r.citations)
    }
    bindings = dict.fromkeys(
        r.binding.to_domain()
        for r in (*case.episodes, *case.facts, *case.links, *case.semantics)
        if r.id not in withheld
    )
    return tuple(
        (
            binding,
            RecordBatch(
                episodes=tuple(
                    r.to_domain()
                    for r in case.episodes
                    if r.binding.to_domain() == binding and r.id not in withheld
                ),
                facts=tuple(
                    FactWrite(r.to_domain())
                    for r in case.facts
                    if r.binding.to_domain() == binding and r.id not in withheld
                ),
                links=tuple(r.to_domain() for r in case.links if r.binding.to_domain() == binding),
                semantics=tuple(
                    r.to_domain()
                    for r in case.semantics
                    if r.binding.to_domain() == binding and r.id not in withheld
                ),
            ),
        )
        for binding in bindings
    )


def _validate_inputs(cases: EvaluationCases) -> None:
    _unique(tuple(c.id for c in cases.cases))
    _unique(tuple(c.legacy_id for c in cases.cases if c.legacy_id is not None))
    vectors: dict[str, Vector] = {}
    for case in cases.cases:
        _validate_case(case)
        content = (
            *case.episodes,
            *case.facts,
            *case.semantics,
            *(m.fact for m in case.mutations if isinstance(m, FactUpdateMutation)),
        )
        for text, vector in (
            (case.query, case.query_vector),
            *((r.normalized_text, r.vector) for r in content),
        ):
            if len(vector) != cases.dimensions or not any(vector):
                raise ValueError
            if text in vectors and vectors[text] != vector:
                raise ValueError
            vectors[text] = vector


def parse_evaluation_inputs(cases_json: str) -> EvaluationCases:
    """Input-only validation for providers; never opens or constructs gold."""
    try:
        cases = EvaluationCases.model_validate_json(cases_json)
        _validate_inputs(cases)
        return cases
    except (ValueError, KeyError, TypeError, OverflowError):
        raise EvaluationDataError("Invalid semantic evaluation inputs") from None


def _validate(cases: EvaluationCases, expectations: EvaluationExpectations) -> None:
    _validate_inputs(cases)
    if {c.id for c in cases.cases} != _unique(tuple(g.id for g in expectations.cases)):
        raise ValueError
    by_id = {c.id: c for c in cases.cases}
    for gold in expectations.cases:
        case = by_id[gold.id]
        candidates = {r.id for r in (*case.episodes, *case.semantics)}
        facts = {r.id for r in case.facts}
        relevant, forbidden = _unique(gold.relevant_ids), _unique(gold.forbidden_ids)
        required_facts, forbidden_facts = (
            _unique(gold.required_fact_ids),
            _unique(gold.forbidden_fact_ids),
        )
        dispatched = _unique(gold.dispatch.memory_ids)
        if (
            not relevant | forbidden | dispatched <= candidates
            or relevant & forbidden
            or not required_facts | forbidden_facts <= facts
            or required_facts & forbidden_facts
            or gold.no_match != (not relevant)
            or not dispatched <= relevant
            or (not gold.dispatch.valid and dispatched)
        ):
            raise ValueError
        if gold.expected_order is not None and _unique(gold.expected_order) != relevant:
            raise ValueError


def parse_evaluation_cases(cases_json: str, expectations_json: str) -> SemanticEvaluationData:
    """Parse and cross-validate two JSON strings without exposing bodies in errors."""
    try:
        cases = EvaluationCases.model_validate_json(cases_json)
        expectations = EvaluationExpectations.model_validate_json(expectations_json)
        _validate(cases, expectations)
        return SemanticEvaluationData(cases, expectations)
    except (ValueError, KeyError, TypeError, OverflowError):
        raise EvaluationDataError("Invalid semantic evaluation data") from None


def load_evaluation_cases(cases_path: Path, expectations_path: Path) -> SemanticEvaluationData:
    """Load separate UTF-8 files; callers cannot accidentally feed gold as cases."""
    try:
        return parse_evaluation_cases(
            cases_path.read_text(encoding="utf-8"), expectations_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError):
        raise EvaluationDataError("Invalid semantic evaluation data") from None

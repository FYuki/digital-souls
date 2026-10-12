"""Strict, evaluation-only formation gold. No scorer, prompt or public API."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .memory_records import PartialDateTime, TemporalValue, TimePrecision
from .privacy_evaluation_cases import Identifier, Strict, Versioned, _unique_object

type Dataset = Literal[
    "memory_extraction",
    "episodic_quality",
    "episodic_actor",
    "semantic_memory",
    "semantic_boundary",
]
type Category = Literal[
    "episodic_event",
    "user_preference",
    "interaction_preference",
    "none",
    "selection",
    "what_where_why",
    "temporal",
    "context",
    "links",
    "evidence",
    "assistant_retelling",
    "assistant_self",
    "multiple_actors",
    "named_third_person",
    "quoted_first_person",
    "quoted_second_person",
    "third_person_reference",
    "unknown_actor",
    "user_context",
    "user_first_person",
    "direct",
    "event_exclusion",
    "confirmation",
    "grounding_exclusion",
    "safety",
    "event_existing_boundary",
]
type Text = Annotated[str, Field(min_length=1, repr=False)]
type Nonnegative = Annotated[int, Field(ge=0)]


class Turn(Strict):
    role: Literal["user", "assistant"]
    text: Text
    stated_at: str | None = None

    @model_validator(mode="after")
    def valid(self) -> "Turn":
        if not self.text.strip():
            raise ValueError("blank formation body")
        if self.stated_at is not None:
            try:
                value = datetime.fromisoformat(self.stated_at)
            except ValueError:
                raise ValueError("invalid source time") from None
            if value.utcoffset() is None:
                raise ValueError("aware source time required")
        return self


class Case(Strict):
    id: Identifier
    turns: Annotated[list[Turn], Field(min_length=1)]


class CaseSet(Versioned):
    dataset: Dataset
    cases: Annotated[list[Case], Field(min_length=1)]

    @model_validator(mode="after")
    def unique(self) -> "CaseSet":
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate formation input id")
        return self


class TextMatch(Strict):
    """Fixed PoC operator, not a new choice of free-text scoring."""

    mode: Literal["equals_any", "contains_any", "null", "rubric_topic"]
    values: list[Text]

    @model_validator(mode="after")
    def consistent(self) -> "TextMatch":
        if (self.mode == "null") != (not self.values):
            raise ValueError("text match requires choices except for null")
        if any(not v.strip() for v in self.values) or len(self.values) != len(set(self.values)):
            raise ValueError("blank or duplicate text choice")
        return self


class FieldMatch(Strict):
    path: Literal[
        "five_w.predicate",
        "five_w.object",
        "five_w.who",
        "five_w.where",
        "five_w.why.text",
        "five_w.when",
        "proposition.subject",
        "proposition.attribute",
        "proposition.value",
        "normalized_text",
    ]
    match: TextMatch

    @model_validator(mode="after")
    def typed(self) -> "FieldMatch":
        if self.path == "five_w.when" and self.match.mode != "null":
            raise ValueError("use typed temporal expectation for known time")
        if self.match.mode == "null" and self.path in {
            "five_w.predicate",
            "proposition.subject",
            "proposition.attribute",
            "proposition.value",
            "normalized_text",
        }:
            raise ValueError("mandatory record text cannot be null")
        return self


class PartialTime(Strict):
    precision: Literal["year", "month", "day", "hour", "minute", "second"]
    year: int
    month: int | None = None
    day: int | None = None
    hour: int | None = None
    minute: int | None = None
    second: int | None = None

    def canonical(self) -> PartialDateTime:
        return PartialDateTime(
            precision=TimePrecision(self.precision),
            year=self.year,
            month=self.month,
            day=self.day,
            hour=self.hour,
            minute=self.minute,
            second=self.second,
        )

    @model_validator(mode="after")
    def valid(self) -> "PartialTime":
        self.canonical()
        return self


class TimeMatch(Strict):
    start: PartialTime | None = None
    end: PartialTime | None = None
    timezone: str | None = None

    @model_validator(mode="after")
    def canonical(self) -> "TimeMatch":
        TemporalValue(
            start=self.start.canonical() if self.start else None,
            end=self.end.canonical() if self.end else None,
            timezone=self.timezone,
        )
        return self


class Actor(Strict):
    """PoC identity correspondence for Who; product FiveW.who remains text."""

    entity_id: Literal["speaker:user", "character:miori"] | None = None
    names: Annotated[list[Text], Field(min_length=1)]

    @model_validator(mode="after")
    def valid(self) -> "Actor":
        if any(not x.strip() for x in self.names) or len(set(self.names)) != len(self.names):
            raise ValueError("invalid actor names")
        return self


class RecordMatch(Strict):
    fields: list[FieldMatch] = Field(default_factory=list)
    target_time: TimeMatch | None = None
    episode_context: Literal["actual", "hypothetical", "fiction"] | None = None
    who: list[Actor] | None = None

    @model_validator(mode="after")
    def consistent(self) -> "RecordMatch":
        paths = [f.path for f in self.fields]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate or contradictory record field")
        if self.target_time is not None and "five_w.when" in paths:
            raise ValueError("conflicting typed and field time")
        if self.who is not None:
            names = [name for actor in self.who for name in actor.names]
            ids = [a.entity_id for a in self.who if a.entity_id is not None]
            if len(names) != len(set(names)) or len(ids) != len(set(ids)):
                raise ValueError("duplicate actor identity")
            if "five_w.who" in paths:
                raise ValueError("conflicting Who constraints")
        if (
            not self.fields
            and self.target_time is None
            and self.episode_context is None
            and self.who is None
        ):
            raise ValueError("empty record constraint")
        return self


class CitationRange(Strict):
    """Eligible character interval, not a requirement to quote an entire turn."""

    turn: Nonnegative
    speaker: Literal["user", "assistant"]
    start: Nonnegative
    end: Nonnegative

    @model_validator(mode="after")
    def ordered(self) -> "CitationRange":
        if self.start >= self.end:
            raise ValueError("empty or reversed citation range")
        return self


class Target(Strict):
    predicate: Text
    object: Text

    @model_validator(mode="after")
    def nonblank(self) -> "Target":
        if not self.predicate.strip() or not self.object.strip():
            raise ValueError("blank evaluation selector")
        return self


class Expectation(Strict):
    id: Identifier
    category: Category
    scope: Literal["type_and_topic", "fact_extraction", "fact_actor", "semantic_admission"]
    selector: Target | None = None
    record_kind: Literal["episode", "fact", "semantic"]
    count: Nonnegative | None = None
    count_scope: Literal["selected_kind", "all_records"] = "selected_kind"
    operation: Literal["NEW"] | None = None
    formation_type: Literal["direct_extraction"] | None = None
    records: list[RecordMatch]
    citations: list[CitationRange]
    required_user_turn: Nonnegative | None = None
    evidence_tokens: list[Text] = Field(default_factory=list)
    linked: bool = False
    link_topics: list[Text] = Field(default_factory=list)
    forbidden_save: bool = False
    poc_gold_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def consistent(self) -> "Expectation":
        if self.count_scope == "all_records" and self.count != 0:
            raise ValueError("all-record zero expectation required")
        if self.scope != "type_and_topic" and self.count is None:
            raise ValueError("component count required")
        if self.scope in {"fact_extraction", "fact_actor"} and self.record_kind != "fact":
            raise ValueError("Fact component kind mismatch")
        if self.scope == "semantic_admission" and self.record_kind != "semantic":
            raise ValueError("Semantic component kind mismatch")
        if self.count == 0:
            if (
                self.records
                or self.operation
                or self.formation_type
                or self.required_user_turn is not None
                or self.linked
            ):
                raise ValueError("zero count contradicts positive gold")
        elif self.count is not None and self.count != len(self.records):
            raise ValueError("count differs from record constraints")
        if self.count != 0 and not self.records:
            raise ValueError("positive gold requires record constraints")
        if self.forbidden_save and self.count != 0:
            raise ValueError("forbidden save requires zero count")
        if self.formation_type is not None and self.record_kind != "semantic":
            raise ValueError("formation type applies to Semantic")
        if self.formation_type is not None and self.required_user_turn is None:
            raise ValueError("direct extraction requires user grounding")
        for record in self.records:
            if self.record_kind == "semantic":
                if (
                    record.target_time is not None
                    or record.episode_context
                    or record.who is not None
                ):
                    raise ValueError("Semantic has incompatible record constraints")
                if any(f.path.startswith("five_w.") for f in record.fields):
                    raise ValueError("Semantic cannot have FiveW constraints")
            elif any(f.path.startswith("proposition.") for f in record.fields):
                raise ValueError("Episode or Fact cannot have Proposition constraints")
        if self.scope == "semantic_admission" and self.count != 0:
            if self.formation_type != "direct_extraction" or self.required_user_turn is None:
                raise ValueError("direct Semantic requires user grounding")
        if self.linked != bool(self.link_topics) or (self.linked and self.record_kind != "fact"):
            raise ValueError("inconsistent Episode Fact links")
        if self.scope == "fact_actor" and (
            self.record_kind != "fact" or any(r.who is None for r in self.records)
        ):
            raise ValueError("actor evaluation requires Fact Who")
        if self.scope == "fact_actor" and self.selector is None:
            raise ValueError("actor target selector required")
        if self.count != 0 and not self.citations:
            raise ValueError("positive expectation requires eligible citations")
        addresses = [(c.turn, c.speaker, c.start, c.end) for c in self.citations]
        if len(set(addresses)) != len(addresses):
            raise ValueError("duplicate eligible citation")
        for values in (self.evidence_tokens, self.link_topics):
            if len(values) != len(set(values)) or any(not x.strip() for x in values):
                raise ValueError("invalid evidence choices")
        return self


class ExpectationSet(Versioned):
    dataset: Dataset
    expectations: Annotated[list[Expectation], Field(min_length=1)]

    @model_validator(mode="after")
    def unique(self) -> "ExpectationSet":
        if len({g.id for g in self.expectations}) != len(self.expectations):
            raise ValueError("duplicate formation gold id")
        return self


class EvaluationCases(Strict):
    cases: CaseSet
    expectations: ExpectationSet

    @model_validator(mode="after")
    def correspond(self) -> "EvaluationCases":
        gold = {g.id: g for g in self.expectations.expectations}
        if self.cases.dataset != self.expectations.dataset:
            raise ValueError("formation datasets differ")
        if {c.id for c in self.cases.cases} != set(gold):
            raise ValueError("formation input and gold id sets differ")
        for case in self.cases.cases:
            expected = gold[case.id]
            for citation in expected.citations:
                if citation.turn >= len(case.turns):
                    raise ValueError("citation addresses missing turn")
                turn = case.turns[citation.turn]
                if citation.speaker != turn.role or citation.end > len(turn.text):
                    raise ValueError("citation speaker or bounds differ from source")
            if expected.required_user_turn is not None and not any(
                c.turn == expected.required_user_turn and c.speaker == "user"
                for c in expected.citations
            ):
                raise ValueError("required user citation missing")
            if expected.evidence_tokens and not any(
                token in case.turns[c.turn].text[c.start : c.end]
                for token in expected.evidence_tokens
                for c in expected.citations
            ):
                raise ValueError("evidence token not in eligible citation")
        return self

    @property
    def version(self) -> str:
        normalized = json.dumps(
            self.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(normalized.encode()).hexdigest()


def load_cases(cases_path: Path, expectations_path: Path) -> EvaluationCases:
    """Suppress bodies, paths and raw parse/validation errors at the file boundary."""
    try:
        return EvaluationCases.model_validate(
            {
                "cases": json.loads(cases_path.read_bytes(), object_pairs_hook=_unique_object),
                "expectations": json.loads(
                    expectations_path.read_bytes(), object_pairs_hook=_unique_object
                ),
            }
        )
    except (OSError, ValueError, RecursionError):
        raise ValueError("invalid formation evaluation cases") from None

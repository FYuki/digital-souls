"""Strict PoC privacy corpus migration contract; no scoring or provider execution."""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")]
type Group = Literal["HEALTH", "MENTAL_STATE", "SELF_HARM", "ABUSE", "FINANCIAL", "NONE"]
type Subject = Literal["SELF", "THIRD_PARTY", "GENERAL"]
type Classification = Literal["SENSITIVE", "NOT_SENSITIVE", "ABSTAIN"]
type Category = Literal[
    "HEALTH", "MENTAL_STATE", "SELF_HARM", "ABUSE", "FINANCIAL", "THIRD_PARTY_PRIVATE", "NONE"
]
type PoCTopic = Literal[
    "HEALTH", "MENTAL_STATE", "SELF_HARM", "ABUSE_OR_SEXUAL_VIOLENCE", "FINANCIAL_SITUATION", "NONE"
]

TOPIC_GROUP: dict[PoCTopic, Group] = {
    "HEALTH": "HEALTH",
    "MENTAL_STATE": "MENTAL_STATE",
    "SELF_HARM": "SELF_HARM",
    "ABUSE_OR_SEXUAL_VIOLENCE": "ABUSE",
    "FINANCIAL_SITUATION": "FINANCIAL",
    "NONE": "NONE",
}


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)


class Case(Strict):
    id: Identifier
    pair_id: Identifier
    language: Literal["ja", "en"]
    text: Annotated[str, Field(min_length=1, repr=False)]
    tags: list[Identifier]

    @model_validator(mode="after")
    def nonblank(self) -> "Case":
        if not self.text.strip():
            raise ValueError("blank privacy case text")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("duplicate privacy case tag")
        return self


class Versioned(Strict):
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        # Literal[1] alone also accepts True / 1.0 under Pydantic strict mode.
        if type(value) is not int:
            raise ValueError("integer schema version required")
        return value


class CaseSet(Versioned):
    cases: Annotated[list[Case], Field(min_length=1)]

    @model_validator(mode="after")
    def complete(self) -> "CaseSet":
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate privacy case id")
        pairs: dict[str, list[Case]] = {}
        for case in self.cases:
            pairs.setdefault(case.pair_id, []).append(case)
        if any(
            len(pair) != 2 or {c.language for c in pair} != {"ja", "en"} for pair in pairs.values()
        ):
            raise ValueError("privacy pair requires exactly one Japanese and one English case")
        return self


class PoCReference(Strict):
    id: Identifier
    pair_id: Identifier
    language: Literal["ja", "en"]
    topic_category: PoCTopic
    synthetic: Literal[True]
    case_kind: Literal["NORMAL"]

    @field_validator("synthetic", mode="before")
    @classmethod
    def strict_synthetic(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("boolean synthetic flag required")
        return value


class Expectation(Strict):
    id: Identifier
    group: Group
    sensitive: bool
    allowed_classifications: Annotated[list[Classification], Field(min_length=1)]
    expected_subject: Subject
    allowed_categories: Annotated[list[Category], Field(min_length=1)]
    poc: PoCReference

    @model_validator(mode="after")
    def consistent(self) -> "Expectation":
        if self.id != self.poc.id or self.group != TOPIC_GROUP[self.poc.topic_category]:
            raise ValueError("inconsistent privacy PoC correspondence")
        if any(
            len(values) != len(set(values))
            for values in (self.allowed_classifications, self.allowed_categories)
        ):
            raise ValueError("duplicate privacy allowed value")
        definite = "SENSITIVE" if self.sensitive else "NOT_SENSITIVE"
        # Schema 1 fixes PoC's exact two choices. ABSTAIN is scored by ADR 0032 decision C
        # (separate count, < 10% per run) in the harness, not by these allowed values.
        if set(self.allowed_classifications) != {definite, "ABSTAIN"}:
            raise ValueError("privacy classifications contradict PoC sensitivity")
        if self.sensitive:
            if self.group == "NONE":
                raise ValueError("sensitive privacy case requires a sensitive group")
            categories: set[Category] = {self.group}
            if self.expected_subject == "THIRD_PARTY":
                categories.add("THIRD_PARTY_PRIVATE")
        else:
            categories = {"NONE"}
        if set(self.allowed_categories) != categories:
            raise ValueError("privacy categories contradict Assessment or migration rules")
        return self


class ExpectationSet(Versioned):
    expectations: Annotated[list[Expectation], Field(min_length=1)]

    @model_validator(mode="after")
    def unique(self) -> "ExpectationSet":
        if len({g.id for g in self.expectations}) != len(self.expectations):
            raise ValueError("duplicate privacy expectation id")
        return self


class EvaluationCases(Strict):
    cases: CaseSet
    expectations: ExpectationSet

    @model_validator(mode="after")
    def correspond(self) -> "EvaluationCases":
        gold = {g.id: g for g in self.expectations.expectations}
        if {c.id for c in self.cases.cases} != set(gold):
            raise ValueError("privacy input and gold id sets differ")
        pairs: dict[str, list[Expectation]] = {}
        for case in self.cases.cases:
            expected = gold[case.id]
            if (case.pair_id, case.language) != (expected.poc.pair_id, expected.poc.language):
                raise ValueError("privacy input and PoC pair metadata differ")
            pairs.setdefault(case.pair_id, []).append(expected)
        for pair in pairs.values():
            first, second = pair
            fields = (
                "group",
                "sensitive",
                "expected_subject",
                "allowed_classifications",
                "allowed_categories",
            )
            if any(getattr(first, field) != getattr(second, field) for field in fields):
                raise ValueError("privacy Japanese and English pair gold differ")
        return self

    @property
    def version(self) -> str:
        """Hash both validated documents, preserving array order and original text."""
        normalized = json.dumps(
            self.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate privacy JSON field")
        result[key] = value
    return result


def load_cases(cases_path: Path, expectations_path: Path) -> EvaluationCases:
    """Load and cross-check input/gold without exposing bodies, paths, or raw errors."""
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
        raise ValueError("invalid privacy evaluation cases") from None

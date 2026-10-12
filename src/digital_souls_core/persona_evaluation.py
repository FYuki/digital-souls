"""Strict deterministic persona scoring; judges only supply qualitative reports."""

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import CompletionInput

CATEGORIES = ("self_intro", "preferences", "world_lore")

type Category = Literal["self_intro", "preferences", "world_lore"]
type Gate = Literal["persona_break"]
type Text = Annotated[str, Field(min_length=1)]
type Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")]
type Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)


class ProbeMessage(Strict):
    role: Literal["user", "assistant"]
    content: Text

    @model_validator(mode="after")
    def nonblank(self) -> "ProbeMessage":
        if not self.content.strip():
            raise ValueError("empty message")
        return self


class Forbidden(Strict):
    text: Text
    gate: Gate | None = None


class Case(Strict):
    id: Identifier
    category: Category
    messages: Annotated[list[ProbeMessage], Field(min_length=1)]
    required: Annotated[list[Annotated[list[Text], Field(min_length=1)]], Field(min_length=1)]
    forbidden: list[Forbidden]
    card_fields: Annotated[list[Text], Field(min_length=1)]

    @model_validator(mode="after")
    def valid(self) -> "Case":
        if any(not x.strip() for x in self.card_fields) or len(set(self.card_fields)) != len(
            self.card_fields
        ):
            raise ValueError("invalid Card annotations")
        expressions = [normalize(x) for group in self.required for x in group]
        expressions += [normalize(x.text) for x in self.forbidden]
        if any(not x.strip() for x in expressions) or len(expressions) != len(set(expressions)):
            raise ValueError("empty or duplicate expression")
        if self.messages[-1].role != "user":
            raise ValueError("probe must end with user input")
        CompletionInput.model_validate({"messages": [m.model_dump() for m in self.messages]})
        return self


class CaseSet(Strict):
    schema_version: Literal[1]
    draft: bool = False
    cases: Annotated[list[Case], Field(min_length=1)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("integer schema version required")
        return value

    @model_validator(mode="after")
    def complete(self) -> "CaseSet":
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate case id")
        if {c.category for c in self.cases} != set(CATEGORIES):
            raise ValueError("all three categories required")
        return self

    @property
    def version(self) -> str:
        fixed = json.dumps(
            self.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(fixed.encode()).hexdigest()


def load_cases(path: Path) -> CaseSet:
    try:
        return CaseSet.model_validate_json(path.read_bytes())
    except Exception:
        raise ValueError("invalid persona cases") from None


class Observation(Strict):
    entry: Identifier
    run: Annotated[int, Field(gt=0)]
    case_id: Identifier
    text: str | None = Field(repr=False)
    card_sha256: Hash | None
    status: Literal["RUN", "NOT RUN", "ERROR"] = "RUN"
    reason: Identifier | None = None

    @model_validator(mode="after")
    def valid(self) -> "Observation":
        if self.status == "RUN" and self.text is None:
            raise ValueError("missing response")
        if self.status != "RUN" and self.reason is None:
            raise ValueError("missing failure reason")
        return self


def score(case: Case, text: str) -> dict[str, Any]:
    body = normalize(text)
    missing = [
        i for i, group in enumerate(case.required) if not any(normalize(x) in body for x in group)
    ]
    matched = [
        i for i, expression in enumerate(case.forbidden) if normalize(expression.text) in body
    ]
    gates = sorted({gate for i in matched if (gate := case.forbidden[i].gate) is not None})
    return {
        "passed": not missing and not matched,
        "missing_groups": missing,
        "forbidden_indices": matched,
        "gates": gates,
    }


class Judge(Protocol):
    @property
    def identity(self) -> dict[str, str]: ...
    async def estimate(self, responses: Sequence[str]) -> dict[str, str]: ...


class FakeJudge:
    @property
    def identity(self) -> dict[str, str]:
        return {
            "id": "fake-egogram-v1",
            "model": "fixture",
            "prompt": "no-inference-v1",
            "settings": "deterministic-v1",
        }

    async def estimate(self, responses: Sequence[str]) -> dict[str, str]:
        return dict.fromkeys(("CP", "NP", "A", "FC", "AC"), "fixture: unestimated")


def aggregate(
    cases: CaseSet,
    observations: list[Observation],
    *,
    entries: tuple[str, ...],
    runs: int,
    commit: str,
    commit_dirty: bool = False,
    mismatch_policy: Literal["warn", "gate"] = "warn",
    include_responses: bool = False,
) -> dict[str, Any]:
    cases = CaseSet.model_validate(cases.model_dump())
    if type(runs) is not int or runs < 1 or not entries or len(set(entries)) != len(entries):
        raise ValueError("invalid evaluation matrix")
    if mismatch_policy not in ("warn", "gate") or re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise ValueError("invalid evaluation configuration")
    observations = [Observation.model_validate(x.model_dump()) for x in observations]
    expected = {
        (entry, run, case.id)
        for entry in entries
        for run in range(1, runs + 1)
        for case in cases.cases
    }
    actual = {(x.entry, x.run, x.case_id) for x in observations}
    if actual != expected or len(observations) != len(expected):
        raise ValueError("incomplete or duplicate evaluation matrix")
    indexed = {c.id: c for c in cases.cases}
    scored: dict[tuple[str, int, str], bool] = {}
    gates: dict[str, list[dict[str, Any]]] = {
        name: []
        for name in (
            "persona_break",
            "card_hash_mismatch",
            "card_hash_missing",
            "execution_incomplete",
        )
    }
    details = []
    summaries = []
    for x in observations:
        address = {"entry": x.entry, "run": x.run, "case_id": x.case_id}
        result = score(indexed[x.case_id], x.text or "")
        key = (x.entry, x.run, x.case_id)
        scored[key] = x.status == "RUN" and result["passed"]
        for gate in result["gates"]:
            gates[gate].append(address)
        if x.status != "RUN":
            gates["execution_incomplete"].append(address)
        if x.card_sha256 is None:
            gates["card_hash_missing"].append(address)
        details.append(
            {
                **address,
                "status": "PASS" if scored[key] else "FAIL",
                "execution": x.status,
                "reason": x.reason,
                "card_sha256": x.card_sha256,
                "missing_groups": result["missing_groups"],
                "forbidden_indices": result["forbidden_indices"],
            }
        )
    for entry in entries:
        for run in range(1, runs + 1):
            current = [x for x in observations if x.entry == entry and x.run == run]
            for category in CATEGORIES:
                subset = [x for x in current if indexed[x.case_id].category == category]
                passed = sum(scored[(entry, run, x.case_id)] for x in subset)
                summaries.append(
                    {
                        "entry": entry,
                        "category": category,
                        "run": run,
                        "total": len(subset),
                        "passed": passed,
                        "rate": passed / len(subset),
                        "status": "PASS" if passed * 10 >= len(subset) * 9 else "FAIL",
                        "card_hashes": sorted(
                            {x.card_sha256 for x in subset if x.card_sha256 is not None}
                        ),
                    }
                )
    by_address = {(x.entry, x.run, x.case_id): x for x in observations}
    for run in range(1, runs + 1):
        current = [x for x in observations if x.run == run and x.card_sha256 is not None]
        hashes = sorted({x.card_sha256 for x in current if x.card_sha256 is not None})
        if len(hashes) > 1:
            gates["card_hash_mismatch"].append(
                {
                    "run": run,
                    "case_ids": sorted({x.case_id for x in current}),
                    "entries": sorted({x.entry for x in current}),
                    "hashes": hashes,
                }
            )
    mismatches = [
        {
            "run": run,
            "case_id": c.id,
            "entries": {entry: scored[(entry, run, c.id)] for entry in entries},
        }
        for run in range(1, runs + 1)
        for c in cases.cases
        if len({scored[(entry, run, c.id)] for entry in entries}) > 1
        and all(by_address[(entry, run, c.id)].status == "RUN" for entry in entries)
    ]
    gate_report = {
        name: {
            "count": len(items),
            "case_ids": sorted(
                {
                    identifier
                    for item in items
                    for identifier in ([item["case_id"]] if "case_id" in item else item["case_ids"])
                }
            ),
            "violations": items,
        }
        for name, items in gates.items()
    }
    passed = (
        bool(scored)
        and all(x["status"] != "FAIL" for x in summaries)
        and not any(gates.values())
        and (mismatch_policy == "warn" or not mismatches)
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "commit": {"sha": commit, "dirty": commit_dirty},
        "case_version": cases.version,
        "cases_draft": cases.draft,
        "runs": runs,
        "cache": "disabled",
        "threshold": 0.9,
        "quality_evidence": False,
        "status": "PASS" if passed else "FAIL",
        "passed": passed,
        "summaries": summaries,
        "gates": gate_report,
        "cases": details,
        "mismatch": {
            "policy": mismatch_policy,
            "count": len(mismatches),
            "case_ids": sorted({x["case_id"] for x in mismatches}),
            "violations": mismatches,
        },
        "not_run": {"activity": "NOT RUN（未実装の入口）"},
        "egogram": {"real_judge": "NOT RUN"},
    }
    if include_responses:
        report["responses"] = [
            {"entry": x.entry, "run": x.run, "case_id": x.case_id, "text": x.text}
            for x in observations
        ]
    return report

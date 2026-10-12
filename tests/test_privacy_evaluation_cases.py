"""Fixed synthetic privacy corpus and strict rejection contracts; no model calls."""

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from digital_souls_core.privacy_classifier import Assessment
from digital_souls_core.privacy_evaluation_cases import (
    CaseSet,
    EvaluationCases,
    ExpectationSet,
    load_cases,
)
from digital_souls_core.privacy_scan import scan

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "evals/privacy"
pytestmark = pytest.mark.ut


def corpus() -> EvaluationCases:
    return load_cases(DATA / "cases.json", DATA / "expectations.json")


def test_fixed_corpus_counts_and_gold_separation() -> None:
    fixed = corpus()
    assert len(fixed.cases.cases) == len(fixed.expectations.expectations) == 60
    assert Counter(c.language for c in fixed.cases.cases) == {"ja": 30, "en": 30}
    assert len({c.pair_id for c in fixed.cases.cases}) == 30
    gold = fixed.expectations.expectations
    assert Counter(g.group for g in gold) == {
        "HEALTH": 12,
        "FINANCIAL": 12,
        "MENTAL_STATE": 10,
        "NONE": 10,
        "SELF_HARM": 8,
        "ABUSE": 8,
    }
    assert Counter(g.sensitive for g in gold) == {True: 40, False: 20}
    assert all(
        set(c.model_dump()) == {"id", "pair_id", "language", "text", "tags"}
        for c in fixed.cases.cases
    )


def test_every_gold_choice_is_representable_by_production_assessment() -> None:
    for gold in corpus().expectations.expectations:
        for classification in gold.allowed_classifications:
            for category in gold.allowed_categories:
                Assessment.model_validate(
                    {
                        "classification": classification,
                        "subject": "UNKNOWN"
                        if classification == "ABSTAIN"
                        else gold.expected_subject,
                        "category": "UNKNOWN" if classification == "ABSTAIN" else category,
                        "policy_version": "core-privacy-v1",
                    }
                )


@pytest.mark.parametrize("index", range(60))
def test_every_case_reaches_classifier_screening(index: int) -> None:
    checked = scan(corpus().cases.cases[index].text)
    assert not checked.secret and not checked.failed


def test_version_hash_covers_input_and_gold() -> None:
    fixed = corpus()
    normalized = json.dumps(
        fixed.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert fixed.version == hashlib.sha256(normalized).hexdigest()
    # Pin provenance independently of the runtime's hash calculation.
    assert fixed.version == "5a77017bfd9006d42d42c15409124e20932a0fa559da6554be51eb2a244fada1"
    raw = fixed.model_dump()
    raw["cases"]["cases"][0]["text"] += " synthetic addition"
    assert EvaluationCases.model_validate(raw).version != fixed.version
    raw = fixed.model_dump()
    raw["expectations"]["expectations"][0]["allowed_classifications"].reverse()
    raw["expectations"]["expectations"][1]["allowed_classifications"].reverse()
    assert EvaluationCases.model_validate(raw).version != fixed.version
    reversed_keys = dict(reversed(list(fixed.model_dump().items())))
    assert EvaluationCases.model_validate(reversed_keys).version == fixed.version


@pytest.mark.parametrize(
    "filename,model",
    [
        ("cases.schema.json", CaseSet),
        ("expectations.schema.json", ExpectationSet),
    ],
)
def test_committed_schema_matches_validator(
    filename: str, model: type[CaseSet | ExpectationSet]
) -> None:
    assert json.loads((DATA / filename).read_text(encoding="utf-8")) == model.model_json_schema()


def mutate(raw: dict[str, Any], change: str) -> None:
    cases = raw["cases"]["cases"]
    gold = raw["expectations"]["expectations"]
    if change == "empty-input":
        raw["cases"]["cases"] = []
    elif change == "empty-gold":
        raw["expectations"]["expectations"] = []
    elif change == "duplicate-input":
        cases.append(cases[0])
    elif change == "duplicate-gold":
        gold.append(gold[0])
    elif change == "id-mismatch":
        gold.pop()
    elif change == "unknown-language":
        cases[0]["language"] = "fr"
    elif change == "blank":
        cases[0]["text"] = " \n\t "
    elif change == "nonstring-text":
        cases[0]["text"] = 123
    elif change == "extra-input":
        cases[0]["sensitive"] = True
    elif change == "extra-gold":
        gold[0]["judge"] = True
    elif change == "extra-poc":
        gold[0]["poc"]["private"] = True
    elif change.startswith("version-"):
        target = "cases" if "input" in change else "expectations"
        raw[target]["schema_version"] = True if "bool" in change else 1.0
    elif change == "unknown-group":
        gold[0]["group"] = "OTHER"
    elif change == "unknown-subject":
        gold[0]["expected_subject"] = "UNKNOWN"
    elif change == "unknown-poc-topic":
        gold[0]["poc"]["topic_category"] = "OTHER"
    elif change == "nonsynthetic":
        gold[0]["poc"]["synthetic"] = False
    elif change == "synthetic-int":
        gold[0]["poc"]["synthetic"] = 1
    elif change == "sensitive-int":
        gold[0]["sensitive"] = 1
    elif change == "poc-id":
        gold[0]["poc"]["id"] = "other"
    elif change == "poc-pair":
        gold[0]["poc"]["pair_id"] = "other"
    elif change == "poc-language":
        gold[0]["poc"]["language"] = "en"
    elif change == "poc-group":
        gold[0]["poc"]["topic_category"] = "FINANCIAL_SITUATION"
    elif change == "missing-pair-language":
        cases.pop(1)
        gold.pop(1)
    elif change == "same-pair-language":
        cases[1]["language"] = "ja"
    elif change == "oversized-pair":
        cases[2]["pair_id"] = cases[0]["pair_id"]
    elif change == "pair-gold-disagreement":
        gold[1]["expected_subject"] = "GENERAL"
    elif change == "blank-tag":
        cases[0]["tags"] = ["  "]
    elif change == "duplicate-tag":
        cases[0]["tags"] *= 2
    elif change == "unsafe-classification":
        gold[0]["allowed_classifications"].append("NOT_SENSITIVE")
    elif change == "safe-opposite":
        gold[4]["allowed_classifications"].append("SENSITIVE")
    elif change == "safe-category":
        gold[4]["allowed_categories"] = ["HEALTH"]
    elif change == "sensitive-category-none":
        gold[0]["allowed_categories"] = ["NONE"]
    elif change == "sensitive-group-none":
        gold[0]["group"] = "NONE"
        gold[0]["poc"]["topic_category"] = "NONE"
    elif change == "missing-third-party-category":
        gold[2]["allowed_categories"] = ["HEALTH"]
    elif change == "extra-third-party-category":
        gold[0]["allowed_categories"].append("THIRD_PARTY_PRIVATE")
    elif change == "wrong-category":
        gold[0]["allowed_categories"] = ["FINANCIAL"]
    elif change == "missing-definite":
        gold[0]["allowed_classifications"] = ["ABSTAIN"]
    elif change == "missing-abstain":
        gold[0]["allowed_classifications"] = ["SENSITIVE"]
    else:
        field, action = change.rsplit(":", 1)
        if action == "empty":
            gold[0][field] = []
        elif action == "duplicate":
            gold[0][field] *= 2
        else:
            gold[0][field] = ["UNKNOWN"]


REJECTIONS = [
    "empty-input",
    "empty-gold",
    "duplicate-input",
    "duplicate-gold",
    "id-mismatch",
    "unknown-language",
    "blank",
    "nonstring-text",
    "extra-input",
    "extra-gold",
    "extra-poc",
    "version-input-bool",
    "version-gold-bool",
    "version-input-float",
    "version-gold-float",
    "unknown-group",
    "unknown-subject",
    "unknown-poc-topic",
    "nonsynthetic",
    "synthetic-int",
    "sensitive-int",
    "poc-id",
    "poc-pair",
    "poc-language",
    "poc-group",
    "missing-pair-language",
    "same-pair-language",
    "oversized-pair",
    "pair-gold-disagreement",
    "blank-tag",
    "duplicate-tag",
    "unsafe-classification",
    "safe-opposite",
    "safe-category",
    "sensitive-category-none",
    "sensitive-group-none",
    "missing-third-party-category",
    "extra-third-party-category",
    "wrong-category",
    "missing-definite",
    "missing-abstain",
] + [
    f"{field}:{action}"
    for field in ("allowed_classifications", "allowed_categories")
    for action in ("empty", "duplicate", "unknown")
]


@pytest.mark.parametrize("change", REJECTIONS)
def test_rejection_paths_hide_body(change: str, tmp_path: Path) -> None:
    raw = corpus().model_dump()
    mutate(raw, change)
    with pytest.raises(ValidationError) as error:
        EvaluationCases.model_validate(raw)
    assert corpus().cases.cases[0].text not in str(error.value)
    inputs, gold = tmp_path / "cases.json", tmp_path / "expectations.json"
    inputs.write_text(json.dumps(raw["cases"], ensure_ascii=False), encoding="utf-8")
    gold.write_text(json.dumps(raw["expectations"], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="^invalid privacy evaluation cases$"):
        load_cases(inputs, gold)


@pytest.mark.parametrize("malformation", ["invalid-json", "duplicate-key", "missing-file"])
def test_loader_hides_json_and_filesystem_errors(malformation: str, tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    if malformation == "invalid-json":
        path.write_text('{"text":"synthetic private body",', encoding="utf-8")
    elif malformation == "duplicate-key":
        raw = (DATA / "cases.json").read_text(encoding="utf-8")
        path.write_text(
            raw.replace('"schema_version": 1', '"schema_version": 0, "schema_version": 1'),
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match="^invalid privacy evaluation cases$") as error:
        load_cases(path, DATA / "expectations.json")
    assert str(path) not in str(error.value)
    assert "synthetic private body" not in str(error.value)

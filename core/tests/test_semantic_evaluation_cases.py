"""Frozen synthetic data contracts; no database or learned model is used."""

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from digital_souls_core.memory_record_store import MemoryRecord, RecordBatch
from digital_souls_core.semantic_evaluation_cases import (
    AnswerExpectation,
    EvaluationDataError,
    EvaluationEpisode,
    EvaluationFact,
    EvaluationSemantic,
    FactUpdateMutation,
    load_evaluation_cases,
    parse_evaluation_cases,
    registration_batches,
)

pytestmark = pytest.mark.ut
ROOT = Path(__file__).resolve().parents[1] / "evals" / "semantic"
LEGACY = {
    "synonym-warm-drink-ja",
    "paraphrase-weekend-ja",
    "cross-language-herb",
    "unrelated-observatory",
    "negated-coffee-preference",
    "updated-morning-drink",
    "multisource-picnic",
    "multisource-partial-revocation",
    "private-source",
    "excluded-source",
    "deleted-source",
    "deleted-memory",
    "binding-character",
    "binding-subject",
    "binding-client",
    "long-text-explicit-detail",
    "post-search-revocation",
    "post-answer-revocation",
    "source-epoch-changed",
}


def inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    return json.loads((ROOT / "cases.json").read_text()), json.loads(
        (ROOT / "expectations.json").read_text()
    )


def test_bundled_cases_convert_to_registration_batches() -> None:
    data = load_evaluation_cases(ROOT / "cases.json", ROOT / "expectations.json")
    assert len(data.cases.cases) > 55
    assert {c.legacy_id for c in data.cases.cases if c.legacy_id} == LEGACY
    for case in data.cases.cases:
        batches = registration_batches(case)
        assert batches
        for binding, batch in batches:
            assert isinstance(batch, RecordBatch)
            records: tuple[MemoryRecord, ...] = (
                *batch.episodes,
                *(w.fact for w in batch.facts),
                *batch.links,
                *batch.semantics,
            )
            for record in records:
                assert record.binding == binding
                assert record.version == 1
                assert record.state.value == "active"
                assert "架空" not in repr(record)


def test_category_counts_match_documentation() -> None:
    data = load_evaluation_cases(ROOT / "cases.json", ROOT / "expectations.json")
    counts: Counter[str] = Counter(g.category for g in data.expectations.cases)
    table = dict(
        (category, int(count))
        for category, count in re.findall(
            r"^\| ([a-z_]+) \| (\d+) \|$", (ROOT / "README.md").read_text(), re.MULTILINE
        )
    )
    assert counts == table
    assert all(counts[c] >= 9 for c in ("synonym", "paraphrase", "cross_language", "unrelated"))


def test_inputs_do_not_contain_gold() -> None:
    cases, _ = inputs()
    forbidden = {
        "category",
        "relevant_ids",
        "forbidden_ids",
        "expected_order",
        "no_match",
        "required_facts",
        "forbidden_facts",
        "dispatch",
        "answer",
    }

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(cases)


@pytest.mark.parametrize(
    "invalid",
    [
        "duplicate_case",
        "duplicate_record",
        "unknown_gold",
        "unknown_link",
        "citation_bounds",
        "speaker",
        "binding",
        "zero_vector",
        "nan_vector",
        "dimension",
        "vector_conflict",
        "case_set",
        "gold",
        "nested_gold",
        "empty",
        "state",
        "version",
        "unknown_mutation",
        "extra",
        "strict",
        "evidence",
        "unknown_source",
        "duplicate_gold",
        "gold_overlap",
        "bad_order",
        "naive_time",
        "bad_update",
    ],
)
def test_invalid_data_is_rejected_without_body(invalid: str) -> None:
    cases, gold = inputs()
    c, g = cases["cases"][0], gold["cases"][0]
    record = c["episodes"][0]
    citation = record["citations"][0]
    if invalid == "duplicate_case":
        cases["cases"].append(c)
    elif invalid == "duplicate_record":
        c["episodes"].append(record)
    elif invalid == "unknown_gold":
        g["relevant_ids"].append("unknown")
    elif invalid == "unknown_link":
        c["links"] = [
            {
                "id": "bad-link",
                "version": 1,
                "binding": c["binding"],
                "created_at": record["created_at"],
                "state": "active",
                "episode_id": "unknown",
                "episode_version": 1,
                "fact_id": "unknown",
                "fact_version": 1,
            }
        ]
    elif invalid == "citation_bounds":
        citation["end"] = 100000
    elif invalid == "speaker":
        citation["speaker"] = "assistant"
    elif invalid == "binding":
        citation["binding"]["character_id"] = "other"
    elif invalid == "zero_vector":
        record["vector"] = [0.0] * cases["dimensions"]
    elif invalid == "nan_vector":
        record["vector"][0] = float("nan")
    elif invalid == "dimension":
        record["vector"].pop()
    elif invalid == "vector_conflict":
        c["episodes"][1]["normalized_text"] = record["normalized_text"]
    elif invalid == "case_set":
        gold["cases"].pop()
    elif invalid == "gold":
        c["relevant_ids"] = []
    elif invalid == "nested_gold":
        record["forbidden_ids"] = []
    elif invalid == "empty":
        cases["cases"] = []
        gold["cases"] = []
    elif invalid == "state":
        record["state"] = "suspended"
    elif invalid == "version":
        record["version"] = 2
    elif invalid == "unknown_mutation":
        c["mutations"] = [
            {"phase": "before_search", "op": "delete_conversation", "conversation_id": "unknown"}
        ]
    elif invalid == "extra":
        record["unexpected"] = "do not expose this body"
    elif invalid == "strict":
        record["version"] = "1"
    elif invalid == "evidence":
        s = next(x for x in cases["cases"] if x["semantics"])["semantics"][0]
        s["episode_evidence"][0]["sources"][0]["message_index"] = 999
    elif invalid == "unknown_source":
        citation["conversation_id"] = "unknown"
    elif invalid == "duplicate_gold":
        g["relevant_ids"] *= 2
    elif invalid == "gold_overlap":
        g["forbidden_ids"] = g["relevant_ids"]
    elif invalid == "bad_order":
        g["expected_order"] = ["unknown"]
    elif invalid == "naive_time":
        record["created_at"] = "2026-01-01T00:00:00"
    elif invalid == "bad_update":
        update = next(m for x in cases["cases"] for m in x["mutations"] if m["op"] == "update_fact")
        update["fact"]["version"] = 7
    with pytest.raises(EvaluationDataError) as caught:
        parse_evaluation_cases(json.dumps(cases), json.dumps(gold))
    assert str(caught.value) == "Invalid semantic evaluation data"
    assert caught.value.__cause__ is None


def test_model_repr_and_file_errors_do_not_disclose_body(tmp_path: Path) -> None:
    data = load_evaluation_cases(ROOT / "cases.json", ROOT / "expectations.json")
    assert "架空" not in repr(data)
    with pytest.raises(EvaluationDataError, match="^Invalid semantic evaluation data$"):
        load_evaluation_cases(tmp_path / "missing", ROOT / "expectations.json")


@pytest.mark.parametrize(
    "invalid",
    [
        "inf_vector",
        "bool_vector",
        "schema_bool",
        "citation_epoch",
        "empty_citations",
        "duplicate_conversation",
        "duplicate_message",
        "duplicate_legacy",
        "unknown_dispatch",
        "phase_order",
        "semantic_independence",
        "semantic_binding",
        "link_binding",
        "excluded_evidence",
        "invalid_time",
        "reason_source",
    ],
)
def test_additional_structural_invariants(invalid: str) -> None:
    cases, gold = inputs()
    case = cases["cases"][0]
    record = case["episodes"][0]
    citation = record["citations"][0]
    derived = next(c for c in cases["cases"] if c["id"] == "derived-semantic")
    if invalid == "inf_vector":
        record["vector"][0] = float("inf")
    elif invalid == "bool_vector":
        record["vector"][0] = True
    elif invalid == "schema_bool":
        cases["schema_version"] = True
    elif invalid == "citation_epoch":
        citation["epoch"] = 1
    elif invalid == "empty_citations":
        record["citations"] = []
    elif invalid == "duplicate_conversation":
        case["conversations"].append(case["conversations"][0])
    elif invalid == "duplicate_message":
        case["conversations"][0]["messages"].append(case["conversations"][0]["messages"][0])
    elif invalid == "duplicate_legacy":
        cases["cases"][1]["legacy_id"] = case["legacy_id"]
    elif invalid == "unknown_dispatch":
        gold["cases"][0]["dispatch"]["memory_ids"] = ["unknown"]
    elif invalid == "phase_order":
        case["mutations"] = [
            dict(
                phase=p,
                op="set_private",
                private_mode=True,
                conversation_id=case["conversations"][0]["id"],
            )
            for p in ("after_answer", "before_search")
        ]
    elif invalid == "semantic_independence":
        derived["semantics"][0]["episode_evidence"].pop()
    elif invalid == "semantic_binding":
        derived["semantics"][0]["binding"]["subject"] = "other"
    elif invalid == "link_binding":
        linked = next(c for c in cases["cases"] if c["links"])
        linked["links"][0]["binding"]["client"] = "other"
    elif invalid == "excluded_evidence":
        derived["mutations"] = [
            dict(
                phase="before_search",
                op="exclude_on_append",
                conversation_id="quiet-source-1",
                turn_revision=1,
                message_index=0,
            )
        ]
    elif invalid == "invalid_time":
        record["experience_time"] = dict(start=dict(precision="month", year=2026, month=13))
    elif invalid == "reason_source":
        record["five_w"]["why"] = dict(
            text="理由の合成本文", citations=[{**citation, "conversation_id": "unknown"}]
        )
    with pytest.raises(EvaluationDataError):
        parse_evaluation_cases(json.dumps(cases), json.dumps(gold))


def test_body_and_nested_domain_fields_are_preserved() -> None:
    data = load_evaluation_cases(ROOT / "cases.json", ROOT / "expectations.json")
    for case in data.cases.cases:
        records: tuple[EvaluationEpisode | EvaluationFact | EvaluationSemantic, ...] = (
            *case.episodes,
            *case.facts,
            *case.semantics,
        )
        for dto in records:
            domain = dto.to_domain()
            assert domain.normalized_text == dto.normalized_text
            assert domain.created_at == dto.created_at
            assert domain.last_user_mentioned_at == dto.last_user_mentioned_at
            assert tuple(c.citation() for c in dto.citations) == domain.citations
        for mutation in case.mutations:
            if isinstance(mutation, FactUpdateMutation):
                assert mutation.batch().facts[0].expected_version == mutation.expected_version
    linked = next(c for c in data.cases.cases if c.id == "valid-fact-attachment")
    episode = linked.episodes[0].to_domain()
    assert episode.experience_time.start is not None
    assert episode.experience_time.start.precision.value == "month"
    assert episode.experience_time.start.day is None
    assert episode.five_w.why is not None
    assert episode.five_w.why.text == "旅の計画を共有したいから"
    fact = linked.facts[0].to_domain()
    assert fact.target_time.start is not None and fact.target_time.end is not None
    assert fact.target_time.timezone == "Asia/Tokyo"


def test_unmutated_fake_vectors_follow_production_ranking() -> None:
    from digital_souls_core.memory_ranking import EmbeddingSpace, RetrievalPolicy, rank_records
    from digital_souls_core.memory_record_store import RetrievalCandidate

    data = load_evaluation_cases(ROOT / "cases.json", ROOT / "expectations.json")
    gold = {g.id: g for g in data.expectations.cases}
    checked = 0
    for case in data.cases.cases:
        if case.mutations:
            continue
        records: tuple[EvaluationEpisode | EvaluationSemantic, ...] = (
            *case.episodes,
            *case.semantics,
        )
        candidates = tuple(
            RetrievalCandidate(r.to_domain()) for r in records if r.binding == case.binding
        )
        vectors = (
            case.query_vector,
            *(r.vector for r in (*case.episodes, *case.semantics) if r.binding == case.binding),
        )
        ranked = rank_records(
            candidates, vectors, EmbeddingSpace("fixture", "v1", 4), RetrievalPolicy()
        )
        ids = tuple(r.identifier for r in ranked)
        expected = gold[case.id]
        assert set(expected.relevant_ids) <= set(ids)
        assert not set(expected.forbidden_ids).intersection(ids)
        if expected.no_match:
            assert ids == ()
        if expected.expected_order is not None:
            assert ids == expected.expected_order
        checked += 1
    assert checked >= 45


@pytest.mark.parametrize("field", ["required_facts", "forbidden_facts"])
@pytest.mark.parametrize(
    "groups",
    [
        [[]],
        [[""]],
        [[" \t"]],
        ["coffee"],
        [[7]],
        [["coffee", "coffee"]],
        [["Coffee", "ＣＯＦＦＥＥ"]],
        [["coffee"], ["COFFEE"]],
        [["coffee", "コーヒー"], ["コーヒー", "珈琲"]],
    ],
)
def test_answer_fact_groups_reject_invalid_alternatives(field: str, groups: Any) -> None:
    cases, gold = inputs()
    gold["cases"][0]["answer"][field] = groups
    with pytest.raises(EvaluationDataError, match="^Invalid semantic evaluation data$"):
        parse_evaluation_cases(json.dumps(cases), json.dumps(gold))


def test_answer_fact_groups_reject_normalized_required_forbidden_overlap() -> None:
    cases, gold = inputs()
    gold["cases"][0]["answer"].update(
        required_facts=[["Coffee", "コーヒー"]], forbidden_facts=[["ＣＯＦＦＥＥ"]]
    )
    with pytest.raises(EvaluationDataError):
        parse_evaluation_cases(json.dumps(cases), json.dumps(gold))


def test_legacy_answer_gold_schema_is_rejected() -> None:
    cases, gold = inputs()
    gold["schema_version"] = 1
    with pytest.raises(EvaluationDataError):
        parse_evaluation_cases(json.dumps(cases), json.dumps(gold))


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("SCARF and tea", True),
        ("ＳＣＡＲＶＥＳ and ＴＥＡ", True),
        ("マフラーと紅茶", True),
        ("scarf", False),
        ("tea", False),
        ("scarf and tea, with coffee", False),
        ("マフラーと紅茶、珈琲", False),
        ("SCARF TEA ＣＯＦＦＥＥ", False),
    ],
)
def test_answer_fact_matching_uses_all_groups_any_alternative(answer: str, expected: bool) -> None:
    gold = AnswerExpectation.model_validate_json(
        json.dumps(
            dict(
                behavior="grounded",
                required_facts=[["scarf", "scarves", "マフラー"], ["tea", "紅茶"]],
                forbidden_facts=[["coffee", "コーヒー", "珈琲"]],
                discarded=False,
            )
        )
    )
    assert gold.matches_facts(answer) is expected


@pytest.mark.parametrize(
    ("case_id", "answer"),
    [
        ("cross-language-herb", "Mint."),
        ("cross-language-02", "The flute."),
        ("cross-language-03", "Along the beach."),
        ("cross-language-04", "Pumpkin soup."),
        ("cross-language-05", "Lilies."),
        ("cross-language-06", "RED."),
        ("cross-language-07", "マフラーを編みます。"),
        ("cross-language-08", "火曜日です。"),
        ("cross-language-09", "秋です。"),
        ("cross-language-10", "山を描きます。"),
        ("synonym-06", "傘を使います。"),
        ("paraphrase-02", "庭で散歩します。"),
        ("paraphrase-03", "二つの目覚まし時計を置きます。"),
        ("paraphrase-04", "しおりを挟みます。"),
        ("paraphrase-06", "カレンダーに書きます。"),
        ("paraphrase-08", "調理が終わったらすぐに洗います。"),
        ("negated-coffee-preference", "珈琲です。"),
        ("long-text-explicit-detail", "オレンジ色です。"),
        ("direct-semantic", "静かで落ち着いた場所です。"),
    ],
)
def test_bundled_gold_accepts_natural_fact_wordings(case_id: str, answer: str) -> None:
    data = load_evaluation_cases(ROOT / "cases.json", ROOT / "expectations.json")
    gold = next(g for g in data.expectations.cases if g.id == case_id)
    assert gold.answer.matches_facts(answer)


@pytest.mark.parametrize(
    ("case_id", "answer"),
    [
        ("cross-language-02", "A guitar."),
        ("cross-language-08", "水曜日です。"),
        ("paraphrase-03", "目覚まし時計を一つ置きます。"),
        ("paraphrase-08", "料理の翌日に洗います。"),
        ("negated-coffee-preference", "紅茶が苦手です。"),
        ("private-source", "青いメモ帳、以前は紫でした。"),
        ("updated-morning-drink", "ほうじ茶と緑茶です。"),
        ("multisource-partial-revocation", "サンドイッチとリンゴジュースです。"),
    ],
)
def test_bundled_gold_rejects_missing_or_forbidden_facts(case_id: str, answer: str) -> None:
    data = load_evaluation_cases(ROOT / "cases.json", ROOT / "expectations.json")
    gold = next(g for g in data.expectations.cases if g.id == case_id)
    assert not gold.answer.matches_facts(answer)


@pytest.mark.parametrize("answer", ["ｶﾌｪ", "cafe\u0301"])
def test_answer_fact_matching_normalizes_unicode(answer: str) -> None:
    gold = AnswerExpectation.model_validate_json(
        json.dumps(
            dict(
                behavior="grounded",
                required_facts=[["カフェ", "café"]],
                forbidden_facts=[],
                discarded=False,
            )
        )
    )
    assert gold.matches_facts(answer)


@pytest.mark.parametrize(
    "identifier",
    [
        g.id
        for g in load_evaluation_cases(
            ROOT / "cases.json", ROOT / "expectations.json"
        ).expectations.cases
    ],
)
def test_top_one_derivation_agrees_with_all_fixed_dispatch_gold(identifier: str) -> None:
    from digital_souls_core.semantic_evaluation_cases import expected_top_one

    data = load_evaluation_cases(ROOT / "cases.json", ROOT / "expectations.json")
    g = next(g for g in data.expectations.cases if g.id == identifier)
    expected = (g.expected_order or g.relevant_ids or (None,))[0]
    assert expected_top_one(g) == expected
    if g.dispatch.memory_ids:
        assert expected_top_one(g) == g.dispatch.memory_ids[0]

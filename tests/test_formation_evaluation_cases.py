"""Synthetic formation corpus format and migration fidelity, without inference."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from digital_souls_core.formation_evaluation_cases import (
    CaseSet,
    EvaluationCases,
    ExpectationSet,
    load_cases,
)

pytestmark = pytest.mark.ut
DATA = Path(__file__).resolve().parents[1] / "evals/formation"
COUNTS = {
    "memory_extraction": (30, 29),
    "episodic_quality": (170, 42),
    "episodic_actor": (100, 100),
    "semantic_memory": (100, 45),
    "semantic_boundary": (4, 1),
}


PINNED = {
    "memory_extraction": (
        "36fea6dde28493aee6827d3224508a6f833b0d08583321d21490b4c5c5287d13",
        "7a0ca5a5c405212a5d90e2e72e810ec6a645142e072198f943956f63b625ea9b",
    ),
    "episodic_quality": (
        "a7d37f67abd205ec558658c554204a73cd8a636a443485f3c632ad8da76cf1d2",
        "7be0e6c1a8c98a9865fde18ce193d9caf3ddd5e491776d8622733e3941499282",
    ),
    "episodic_actor": (
        "130967b63c373080180f7e2c46fe3c972db6006c1b1795c3da3837dcdf105289",
        "10ba9cfdc9ef58dd128cbcae57a6623d6929d1b03a2badd4b109fb70cc9e0cb6",
    ),
    "semantic_memory": (
        "1603788df455b6598ab328468c9fe048692efce51c851b87ab0749efc6fdd676",
        "41455cfbdfe33d4b628d917e437ce888a9dacbb302d02de7319ecf96f77bf3d9",
    ),
    "semantic_boundary": (
        "d2546ace86fcdf6f86d4309dc393cfdc47b6bba5c0cdaddd49a2e0a80cdd1df7",
        "47d8a1083af4aaba3872ff7ff3c90615b2917623e1c9ef7fd5936f0ad7b818c6",
    ),
}


def corpus(dataset: str = "episodic_quality") -> EvaluationCases:
    return load_cases(DATA / dataset / "cases.json", DATA / dataset / "expectations.json")


@pytest.mark.parametrize("dataset", COUNTS)
def test_fixed_counts_ids_bodies_and_original_gold(dataset: str) -> None:
    fixed = corpus(dataset)
    manifest = json.loads((DATA / "migration.json").read_bytes())[dataset]
    original = DATA / "source" / manifest["file"]
    assert (
        hashlib.sha256(original.read_bytes()).hexdigest()
        == manifest["sha256"]
        == PINNED[dataset][0]
    )
    assert fixed.version == manifest["version"] == PINNED[dataset][1]
    rows = [json.loads(line) for line in original.read_text().splitlines()]
    assert len(rows) == COUNTS[dataset][0]
    assert len(fixed.cases.cases) == len(fixed.expectations.expectations) == COUNTS[dataset][1]
    migrated = {c.id for c in fixed.cases.cases}
    deferred = {entry["id"] for entry in manifest["deferred"]}
    assert not migrated & deferred
    assert migrated | deferred == {r["id"] for r in rows}
    assert len(deferred) == len(manifest["deferred"])
    assert migrated == set(manifest["migrated"])
    assert all(entry["reason"] for entry in manifest["deferred"])
    by_id = {r["id"]: r for r in rows}
    gold = {g.id: g for g in fixed.expectations.expectations}
    for case in fixed.cases.cases:
        row = by_id[case.id]
        if dataset == "memory_extraction":
            turns = [{"role": "user", "text": row["vars"]["text"]}]
            expected = {k: v for k, v in row["vars"].items() if k.startswith("expected_")}
        elif dataset.startswith("episodic_"):
            turns = json.loads(row["vars"]["input_json"])["turns"]
            expected = json.loads(row["vars"]["expected_json"])
        else:
            turns = [
                {"role": role, "text": turn[role]}
                for turn in row["turns"]
                for role in ("user", "assistant")
                if turn.get(role)
            ]
            expected = row["expected"]
        assert [t.model_dump(exclude_none=True) for t in case.turns] == turns
        encoded = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert gold[case.id].poc_gold_sha256 == hashlib.sha256(encoded.encode()).hexdigest()
        assert set(case.model_dump()) == {"id", "turns"}


@pytest.mark.parametrize(
    "filename,model", [("cases.schema.json", CaseSet), ("expectations.schema.json", ExpectationSet)]
)
def test_generated_schemas(filename: str, model: type[CaseSet | ExpectationSet]) -> None:
    assert json.loads((DATA / filename).read_bytes()) == model.model_json_schema()


def changed(change: str) -> dict[str, Any]:
    raw = corpus().model_dump()
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
    elif change == "dataset-mismatch":
        raw["expectations"]["dataset"] = "episodic_actor"
    elif change == "extra-input":
        cases[0]["expected"] = "gold"
    elif change == "unknown-role":
        cases[0]["turns"][0]["role"] = "system"
    elif change == "blank-body":
        cases[0]["turns"][0]["text"] = " \t "
    elif change == "nonstring-body":
        cases[0]["turns"][0]["text"] = 1
    elif change == "empty-turns":
        cases[0]["turns"] = []
    elif change == "version-bool":
        raw["cases"]["schema_version"] = True
    elif change == "count-bool":
        gold[0]["count"] = True
    elif change == "unknown-kind":
        gold[0]["record_kind"] = "reflection"
    elif change == "unknown-category":
        gold[0]["category"] = "future"
    elif change == "unknown-path":
        gold[0]["records"][0]["fields"][0]["path"] = "five_w.guess"
    elif change == "zero-with-record":
        gold[0]["count"] = 0
    elif change == "count-records":
        gold[0]["count"] = 2
    elif change == "contradictory-field":
        gold[0]["records"][0]["fields"].append(
            {"path": "five_w.object", "match": {"mode": "null", "values": []}}
        )
    elif change == "duplicate-choice":
        gold[0]["records"][0]["fields"][0]["match"]["values"] *= 2
    elif change == "empty-choice":
        gold[0]["records"][0]["fields"][0]["match"]["values"] = []
    elif change == "citation-outside":
        gold[0]["citations"][0]["end"] = 999
    elif change == "citation-speaker":
        gold[0]["citations"][0]["speaker"] = "assistant"
    elif change == "citation-turn":
        gold[0]["citations"][0]["turn"] = 99
    elif change == "citation-reversed":
        gold[0]["citations"][0]["start"] = gold[0]["citations"][0]["end"]
    elif change == "missing-count":
        gold[0]["count"] = None
    elif change == "wrong-component-kind":
        gold[0]["record_kind"] = "semantic"
    elif change == "invalid-stated-at":
        cases[0]["turns"][0]["stated_at"] = cases[0]["turns"][0]["text"]
    elif change == "forbidden-with-count":
        gold[0]["forbidden_save"] = True
    elif change == "bad-time":
        gold[0]["records"][0]["target_time"] = {
            "start": {"precision": "month", "year": 2026, "month": 9, "day": 1}
        }
    else:
        raise AssertionError(change)
    return raw


@pytest.mark.parametrize(
    "change",
    [
        "empty-input",
        "empty-gold",
        "duplicate-input",
        "duplicate-gold",
        "id-mismatch",
        "dataset-mismatch",
        "extra-input",
        "unknown-role",
        "blank-body",
        "nonstring-body",
        "empty-turns",
        "version-bool",
        "count-bool",
        "unknown-kind",
        "unknown-category",
        "unknown-path",
        "zero-with-record",
        "count-records",
        "contradictory-field",
        "duplicate-choice",
        "empty-choice",
        "citation-outside",
        "citation-speaker",
        "citation-turn",
        "citation-reversed",
        "forbidden-with-count",
        "bad-time",
        "missing-count",
        "wrong-component-kind",
        "invalid-stated-at",
    ],
)
def test_fail_closed_and_safe_error(change: str, tmp_path: Path) -> None:
    raw = changed(change)
    with pytest.raises(ValidationError) as error:
        EvaluationCases.model_validate(raw)
    assert corpus().cases.cases[0].turns[0].text not in str(error.value)
    cases, gold = tmp_path / "cases.json", tmp_path / "gold.json"
    cases.write_text(json.dumps(raw["cases"]))
    gold.write_text(json.dumps(raw["expectations"]))
    with pytest.raises(ValueError, match="^invalid formation evaluation cases$"):
        load_cases(cases, gold)


@pytest.mark.parametrize(
    "content", ['{"body":"synthetic body",', '{"schema_version":1,"schema_version":1}', None]
)
def test_loader_hides_json_and_filesystem_errors(content: str | None, tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(ValueError, match="^invalid formation evaluation cases$") as error:
        load_cases(path, DATA / "episodic_quality/expectations.json")
    assert "synthetic body" not in str(error.value)
    assert str(path) not in str(error.value)


@pytest.mark.parametrize("dataset", COUNTS)
def test_translated_gold_preserves_every_source_operator(dataset: str) -> None:
    fixed = corpus(dataset)
    original = [
        json.loads(line) for line in (DATA / "source" / f"{dataset}.jsonl").read_text().splitlines()
    ]
    source = {r["id"]: r for r in original}
    for expected in fixed.expectations.expectations:
        row = source[expected.id]
        if dataset == "memory_extraction":
            v = row["vars"]
            assert expected.scope == "type_and_topic"
            if v["expected_memory_type"] == "NONE":
                assert expected.count == 0 and not expected.records
                assert expected.count_scope == "all_records"
            else:
                assert expected.count is None
                assert expected.record_kind == (
                    "episode" if v["expected_memory_type"] == "EPISODIC_EVENT" else "semantic"
                )
                topic = expected.records[0].fields[0]
                assert topic.path == "normalized_text"
                assert topic.match.mode == "rubric_topic"
                assert topic.match.values == [v["expected_topic"]]
        elif dataset == "episodic_actor":
            data = json.loads(row["vars"]["input_json"])
            truth = json.loads(row["vars"]["expected_json"])
            record = expected.records[0]
            assert record.who is not None
            assert [a.model_dump() for a in record.who] == truth
            assert expected.selector is not None
            assert expected.selector.model_dump() == data["target"]
            assert {f.path: f.match.values for f in record.fields} == {
                "five_w.predicate": [data["target"]["predicate"]],
                "five_w.object": [data["target"]["object"]],
            }
        elif dataset == "episodic_quality":
            truth = json.loads(row["vars"]["expected_json"])
            assert expected.count == truth.get("count", 1)
            assert expected.evidence_tokens == truth.get("evidence_tokens", [])
            assert expected.linked == truth.get("linked", False)
            if expected.category == "temporal":
                time = expected.records[0].target_time
                assert time is not None
                fields = truth["fields"]
                if "five_w.when" in fields:
                    assert time.start is None and time.end is None
                else:
                    assert time.start is not None
                    for part in ("year", "month", "day"):
                        assert getattr(time.start, part) == fields[f"five_w.when.parts.{part}"]
                    depth = sum(
                        fields[f"five_w.when.parts.{part}"] is not None
                        for part in ("year", "month", "day")
                    )
                    assert time.start.precision == ("year", "month", "day")[depth - 1]
            elif expected.category == "context":
                assert truth == {"fields": {"five_w.context": "FICTIONAL"}}
                assert expected.records[0].episode_context == "fiction"
            elif expected.count:
                specs = truth.get("records", [truth.get("fields", {})])
                assert len(specs) == len(expected.records)
                for spec, record in zip(specs, expected.records, strict=True):
                    constraints = {f.path: f.match for f in record.fields}
                    for key, val in spec.items():
                        if key == "operation":
                            assert expected.operation == val
                            continue
                        core_path = (
                            key.replace("five_w.what.object", "five_w.object")
                            .replace("five_w.where.name", "five_w.where")
                            .replace("five_w.why", "five_w.why.text")
                        )
                        match = constraints.pop(core_path)
                        assert match.mode == ("null" if val is None else "contains_any")
                        assert match.values == ([] if val is None else val["contains_any"])
                    assert not constraints
                if expected.linked:
                    assert expected.link_topics == [
                        token
                        for spec in specs
                        for token in spec["five_w.what.object"]["contains_any"]
                    ]
        else:
            truth = row["expected"]
            assert expected.count == truth["count"]
            assert expected.forbidden_save == truth.get("forbidden_save", False)
            if expected.count:
                assert expected.operation == truth["operation"]
                assert expected.formation_type == "direct_extraction"
                assert truth["self_report"] is True
                assert expected.required_user_turn == truth.get("source_turn", 0) * 2
                fields = {f.path: f.match for f in expected.records[0].fields}
                assert fields["proposition.value"].values == [truth["value_contains"]]
                assert fields["proposition.value"].mode == "contains_any"
                assert fields["proposition.subject"].values == ["ユーザー"]

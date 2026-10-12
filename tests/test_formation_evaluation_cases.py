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
    "episodic_quality": (170, 94),
    "episodic_actor": (100, 100),
    "semantic_memory": (100, 60),
    "semantic_boundary": (4, 4),
}


PINNED = {
    "memory_extraction": (
        "36fea6dde28493aee6827d3224508a6f833b0d08583321d21490b4c5c5287d13",
        "c0838d37b3a6c13cefa52d8a7b992e1a2645aeb4e22eccb9181d9f3acd3aac44",
    ),
    "episodic_quality": (
        "a7d37f67abd205ec558658c554204a73cd8a636a443485f3c632ad8da76cf1d2",
        "f8c6f25c79f4cb0b556587b029d7b8d68683900c8b6e679a85be6603a34d5c53",
    ),
    "episodic_actor": (
        "130967b63c373080180f7e2c46fe3c972db6006c1b1795c3da3837dcdf105289",
        "ea0cb630dca53efd3b9680787140d155290336369504e3a39df3add9a0ccd14b",
    ),
    "semantic_memory": (
        "1603788df455b6598ab328468c9fe048692efce51c851b87ab0749efc6fdd676",
        "0499a32cdbe2c3b4c580e3909d551e091126b0a02723be42c539c70835d0a8ca",
    ),
    "semantic_boundary": (
        "d2546ace86fcdf6f86d4309dc393cfdc47b6bba5c0cdaddd49a2e0a80cdd1df7",
        "03c3924aa08dbfcadd8720bec959e0c993947b7009e8a9822ae092d0596a797c",
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
        assert [{"role": t.role, "text": t.text} for t in case.turns] == turns
        encoded = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert gold[case.id].poc_gold_sha256 == hashlib.sha256(encoded.encode()).hexdigest()
        assert set(case.model_dump()) == {
            "id",
            "turns",
            "binding",
            "conversation_id",
            "seed",
            "clock_source",
            "timezone",
        }


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
            if expected.category in {"catalog", "same_event", "merge_proposal"}:
                assert expected.count is None and not expected.records
                if "matches" in truth:
                    assert expected.matches is not None
                    assert [
                        [m.target.record_id, m.target.version, m.operation, m.certainty]
                        for m in expected.matches
                    ] == truth["matches"]
                else:
                    assert [t.record_id for t in expected.merge_targets] == truth.get(
                        "merge_targets", []
                    )
                    assert all(t.version == 1 for t in expected.merge_targets)
                    assert expected.no_merges == truth.get("no_merges", False)
                    assert expected.merge_count == (0 if truth.get("no_merges") else 1)
                    assert expected.nonempty_fact == truth.get("nonempty_fact", False)
                continue
            assert expected.count == truth.get("count", 1)
            assert expected.evidence_tokens == truth.get("evidence_tokens", [])
            assert expected.linked == truth.get("linked", False)
            if expected.category == "temporal":
                time = expected.records[0].target_time
                assert time is not None
                fields = truth["fields"]
                if "five_w.when" in fields:
                    assert time.start is None and time.end is None
                elif fields.get("five_w.when.relative_unit"):
                    assert time.timezone == "UTC" and time.start is not None
                    unit = fields["five_w.when.relative_unit"]
                    offset = fields["five_w.when.relative_offset"]
                    assert (
                        time.start.year,
                        time.start.month,
                        time.start.day,
                        time.start.precision,
                    ) == {
                        "DAY": (2026, 9, 13 + offset, "day"),
                        "MONTH": (2026, 9 + offset, None, "month"),
                        "YEAR": (2026 + offset, None, None, "year"),
                    }[unit]
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
                        if key == "target.id":
                            assert expected.target is not None
                            assert expected.target.record_id == val and expected.target.version == 1
                            continue
                        if key == "target":
                            assert val is None and expected.target_null
                            continue
                        if key == "changes":
                            assert expected.changes == val
                            continue
                        if key == "five_w":
                            assert val is None and expected.content_absent
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
                if expected.category == "reaffirm":
                    assert expected.target is not None
                    assert (
                        expected.target.record_id == truth["target"]
                        and expected.target.version == 1
                    )
                    assert expected.required_user_turn == 0
                    continue
                assert truth["self_report"] is True
                assert expected.required_user_turn == truth.get("source_turn", 0) * 2
                fields = {f.path: f.match for f in expected.records[0].fields}
                assert fields["proposition.value"].values == [truth["value_contains"]]
                assert fields["proposition.value"].mode == "contains_any"
                assert fields["proposition.subject"].values == ["ユーザー"]


def test_foreign_binding_seed_is_input_and_never_gold() -> None:
    fixed = corpus("semantic_memory")
    case = next(c for c in fixed.cases.cases if c.id == "safety-06")
    assert case.seed[0].binding.character_id == "other-character"
    assert case.binding.character_id == "miori"
    assert case.seed[0].normalized_text == "別ユーザーの合成知識"
    gold = next(g for g in fixed.expectations.expectations if g.id == case.id)
    assert gold.forbidden_save and gold.count == 0


@pytest.mark.parametrize("change", ["binding", "version", "state", "gold", "clock", "clock-source"])
def test_seed_and_clock_fail_closed(change: str) -> None:
    raw = corpus().model_dump()
    case = next(c for c in raw["cases"]["cases"] if c["id"] == "fact_operation-01")
    seed = case["seed"][0]
    if change == "binding":
        seed["binding"]["character_id"] = "other-character"
    elif change == "version":
        seed["version"] = 2
    elif change == "state":
        seed["state"] = "suspended"
    elif change == "gold":
        seed["expected_operation"] = "UPDATE"
    elif change == "clock":
        case["turns"][0]["stated_at"] = "2026-10-13T00:00:00+00:00"
    else:
        case["clock_source"] = "backend/evals/episodic_quality/provider.py:44"
    with pytest.raises(ValidationError):
        EvaluationCases.model_validate(raw)


@pytest.mark.parametrize("dataset", COUNTS)
def test_seed_projection_and_clock_preserve_only_provider_input(dataset: str) -> None:
    rows = {
        r["id"]: r
        for r in map(json.loads, (DATA / "source" / f"{dataset}.jsonl").read_text().splitlines())
    }
    for case in corpus(dataset).cases.cases:
        row = rows[case.id]
        if dataset == "episodic_quality":
            data = json.loads(row["vars"]["input_json"])
            known = data.get("known", [])
            assert len(case.seed) == len(known)
            assert case.clock_source == "backend/evals/episodic_quality/provider.py:43"
            assert case.timezone == "UTC"
            assert all(t.stated_at == "2026-09-13T00:00:00+00:00" for t in case.turns)
            for seed, original in zip(case.seed, known, strict=True):
                assert seed.record_id == original["id"]
                assert seed.kind == original["kind"].lower()
                assert seed.version == original["content_version"]
                assert seed.state == original["status"].lower()
                assert seed.binding.character_id == original["character_id"]
                assert seed.binding.scope.client == case.binding.scope.client == "formation-fixture"
                assert seed.binding.scope.subject == case.binding.scope.subject == "fixture-user"
                assert seed.conversation_id == original["conversation_id"] == case.conversation_id
                assert seed.created_at == original["created_at"]
                assert seed.updated_at == original["updated_at"]
                assert seed.source_summary.model_dump() == original["source_summary"]
                assert not seed.sources and original["sources"] == []
                assert seed.normalized_text is None and seed.proposition is None
                w, source = seed.five_w, original["five_w"]
                assert w is not None
                assert [p.model_dump() for p in w.who] == source["who"]
                assert w.predicate == source["what"]["predicate"]
                assert w.object == source["what"]["object"]
                assert w.polarity == source["what"]["polarity"]
                assert w.actuality == source["what"]["actuality"]
                assert w.context == source["context"] and w.why == source["why"]
                assert (w.where.model_dump() if w.where else None) == source["where"]
                if source["when"] is None:
                    assert w.when is None
                else:
                    assert w.when is not None and w.when.start is not None
                    for part, value in source["when"]["parts"].items():
                        assert getattr(w.when.start, part) == value
                    assert w.when.end == source["when"]["end"]
                    assert w.when.timezone == source["when"]["timezone"]
                    assert w.when.reference_at == source["when"]["reference_at"]
                    assert w.when.range_kind == source["when"]["range_kind"]
        elif dataset in {"semantic_memory", "semantic_boundary"}:
            assert case.clock_source == "backend/evals/semantic_memory/fixtures.py:24"
            assert case.timezone == "Asia/Tokyo"
            assert all(t.stated_at == "2026-09-15T00:00:00+00:00" for t in case.turns)
            assert len(case.seed) == len(row["existing"])
            for seed, original in zip(case.seed, row["existing"], strict=True):
                assert seed.record_id == original["key"]
                assert seed.kind == "semantic" and seed.version == 1 and seed.state == "active"
                assert seed.binding.character_id == original.get("character_id", "miori")
                assert seed.normalized_text == original["content"]
                assert seed.formation_type == original["formation_type"].lower()
                assert seed.proposition is not None
                assert seed.proposition.model_dump() == {
                    "subject": original["subject"],
                    "attribute": original["predicate"],
                    "value": original["value"],
                    "mutability": original["mutability"],
                    "self_report": original["self_report"],
                }
                assert len(seed.sources) == 1
                assert seed.sources[0].role == "user"
                assert seed.sources[0].text == original["content"]
                assert (
                    seed.sources[0].stated_at
                    == seed.created_at
                    == seed.updated_at
                    == "2026-09-15T00:00:00+00:00"
                )
        else:
            assert not case.seed and case.clock_source is None and case.timezone is None
            assert all(t.stated_at is None for t in case.turns)


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "version-zero",
        "version-bool",
        "unknown-state",
        "body-kind",
        "missing-user",
        "summary",
        "nested-gold",
        "scope-missing",
    ],
)
def test_invalid_seed_state_is_rejected_without_inference(change: str) -> None:
    raw = corpus("semantic_memory").model_dump()
    case = next(c for c in raw["cases"]["cases"] if c["id"] == "safety-06")
    seed = case["seed"][0]
    if change == "duplicate":
        case["seed"].append(seed)
    elif change == "version-zero":
        seed["version"] = 0
    elif change == "version-bool":
        seed["version"] = True
    elif change == "unknown-state":
        seed["state"] = "deleted"
    elif change == "body-kind":
        seed["kind"] = "fact"
    elif change == "missing-user":
        seed["sources"][0]["role"] = "assistant"
    elif change == "summary":
        seed["source_summary"]["count"] = 0
    elif change == "nested-gold":
        seed["proposition"]["expected_value"] = "gold"
    else:
        del seed["binding"]["scope"]["subject"]
    with pytest.raises(ValidationError):
        EvaluationCases.model_validate(raw)


@pytest.mark.parametrize("part", ["subject", "client", "character_id"])
def test_other_binding_components_can_be_seeded_without_becoming_gold_targets(part: str) -> None:
    raw = corpus("semantic_memory").model_dump()
    case = next(c for c in raw["cases"]["cases"] if c["id"] == "safety-06")
    seed = case["seed"][0]
    seed["binding"] = dict(case["binding"], scope=dict(case["binding"]["scope"]))
    if part == "character_id":
        seed["binding"][part] = "other-character"
    else:
        seed["binding"]["scope"][part] = "other-fixture"
    fixed = EvaluationCases.model_validate(raw)
    other = next(c for c in fixed.cases.cases if c.id == case["id"])
    assert other.seed[0].binding != other.binding


def test_seed_identity_is_scoped_even_when_foreign_id_collides() -> None:
    raw = corpus().model_dump()
    case = next(c for c in raw["cases"]["cases"] if c["id"] == "fact_operation-01")
    foreign = dict(case["seed"][0], binding=dict(case["binding"], character_id="other-character"))
    case["seed"].append(foreign)
    assert EvaluationCases.model_validate(raw)
    case["seed"].pop(0)
    with pytest.raises(ValidationError):
        EvaluationCases.model_validate(raw)


@pytest.mark.parametrize("change", ["missing", "wrong-provider"])
def test_clock_provenance_is_required_for_its_original_dataset(change: str) -> None:
    raw = corpus().model_dump()
    case = raw["cases"]["cases"][0]
    case["clock_source"] = (
        None if change == "missing" else "backend/evals/semantic_memory/fixtures.py:24"
    )
    if change == "missing":
        case["timezone"] = None
        for turn in case["turns"]:
            turn["stated_at"] = None
    else:
        case["timezone"] = "Asia/Tokyo"
        for turn in case["turns"]:
            turn["stated_at"] = "2026-09-15T00:00:00+00:00"
    with pytest.raises(ValidationError):
        EvaluationCases.model_validate(raw)

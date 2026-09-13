"""簡素化したLLM出力でも出典と操作の検証を迂回しない。"""
import json
import pytest
from pydantic import ValidationError
from evals.episodic_quality.provider import build_input, anchor_validator
from evals.episodic_quality.compact import Plan, expand, compact_payload
from app.memory.episodic.quotes import InvalidExtraction

def fixture():
    data, _, payload = build_input(json.dumps({
        "turns": [{"role": "user", "text": "私は鍵を拾った。次に財布を届けた。"}],
        "stage": "extract",
    }, ensure_ascii=False))
    return payload, {
        "complete": True,
        "facts": [{"operation": "NEW", "target": None, "predicate": "拾った", "object": "鍵",
                   "anchor": {"quote_index": 0, "text": "私は鍵を拾った。", "start": None}, "changes": []}],
        "episodes": [{"target": None, "topic": "鍵を拾った話",
                      "anchor": {"quote_index": 0, "text": "私は鍵を拾った。", "start": None}, "topic_indices": [0]}],
        "merges": [],
    }

def test_index_mapping_keeps_authoritative_source_identity():
    payload, plan = fixture()
    batch = expand(Plan.model_validate(plan), payload)
    anchor_validator(payload)(batch)
    assert str(batch.records[0].anchor.source_id) == payload["fragments"][0]["source_id"]
    assert batch.records[0].anchor.revision == 2
    assert batch.links[0].fact == "f0"
    assert "source_id" not in compact_payload(payload)["quote_options"][0]

@pytest.mark.parametrize("change", ["source", "link", "operation", "quote", "duplicate"])
def test_invalid_proposals_fail_closed(change):
    payload, plan = fixture()
    if change == "source":
        plan["facts"][0]["anchor"]["quote_index"] = 9
    elif change == "link":
        plan["episodes"][0]["topic_indices"] = [9]
    elif change == "operation":
        plan["facts"][0]["operation"] = "UPDATE"
    elif change == "quote":
        plan["facts"][0]["anchor"]["text"] = "会話にない内容"
    else:
        plan["facts"].append(plan["facts"][0].copy())
    with pytest.raises((InvalidExtraction, ValidationError)):
        batch = expand(Plan.model_validate(plan), payload)
        anchor_validator(payload)(batch)

def test_targets_use_kind_specific_positions_without_inventing_ids():
    from pathlib import Path
    cases = [json.loads(line) for line in
             (Path(__file__).parents[2] / "evals/episodic_quality/cases.jsonl").read_text().splitlines()]
    case = next(c for c in cases if c["id"] == "episode_boundary-01")
    _, _, payload = build_input(case["vars"]["input_json"])
    source = payload["fragments"][0]
    q = {"quote_index": 0, "text": source["text"], "start": None}
    plan = Plan.model_validate({
        "complete": True,
        "facts": [{"operation": "REFERENCE", "target": 0, "predicate": None,
                   "object": None, "anchor": q, "changes": []}],
        "episodes": [{"target": 0, "topic": "体験の続き", "anchor": q, "topic_indices": [0]}],
        "merges": [],
    })
    batch = expand(plan, payload)
    assert str(batch.records[0].target.id) == next(r["id"] for r in payload["known_records"] if r["kind"] == "FACT")
    assert str(batch.records[1].target.id) == next(r["id"] for r in payload["known_records"] if r["kind"] == "EPISODE")


def test_reference_and_single_update_preserve_sources_and_links():
    from pathlib import Path
    cases = [json.loads(line) for line in
             (Path(__file__).parents[2] / "evals/episodic_quality/cases.jsonl").read_text().splitlines()]
    case = next(c for c in cases if c["id"] == "fact_operation-01")
    _, _, payload = build_input(case["vars"]["input_json"])
    from evals.episodic_quality.compact import evidence_units
    units = evidence_units(payload)
    quotes = [{"quote_index": i, "text": unit["text"], "start": None} for i,unit in enumerate(units)]
    plan = Plan.model_validate({
        "complete": True,
        "facts": [
            {"operation": "REFERENCE", "target": 0, "predicate": None, "object": None,
             "anchor": quotes[0], "changes": []},
            {"operation": "UPDATE", "target": 0, "predicate": "食べた", "object": "そば",
             "anchor": quotes[1], "changes": ["what"]},
        ],
        "episodes": [{"target": None, "topic": "訂正", "anchor": quotes[0], "topic_indices": [0,1]}],
        "merges": [],
    })
    batch = expand(plan, payload)
    assert len(batch.records) == 2
    assert batch.records[0].operation == "UPDATE"
    assert len(batch.records[0].sources) == 2
    assert all(link.fact == batch.records[0].key for link in batch.links)
    anchor_validator(payload)(batch)

def test_generation_scope_matches_textual_schema_and_offered_targets():
    from evals.episodic_quality.compact import ScopedClient, FactPlan
    from app.memory.formation.episodic_extractor import generation_schema
    class Client:
        def chat(self, messages, *, json_schema, **kwargs):
            self.messages, self.schema = messages, json_schema
            return "{}"
    delegate = Client()
    payload = {"known_facts": [{"index": 0}], "quote_options": [{}, {}]}
    schema = generation_schema(FactPlan)
    messages = ({"role": "system", "content": "指示\n出力のJSON Schema:\n{}"},
                {"role": "user", "content": json.dumps(payload)})
    ScopedClient(delegate).chat(messages, json_schema=schema, timeout_seconds=1, max_output_tokens=10)
    assert delegate.schema["$defs"]["ReferenceFact"]["properties"]["target"]["enum"] == [0]
    assert delegate.schema["$defs"]["Quote"]["properties"]["quote_index"]["enum"] == [0,1]
    assert json.loads(delegate.messages[0]["content"].split("出力のJSON Schema:\n")[1]) == delegate.schema
    assert "enum" not in schema["$defs"]["ReferenceFact"]["properties"]["target"]

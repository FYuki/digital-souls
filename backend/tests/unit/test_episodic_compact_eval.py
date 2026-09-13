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
                   "anchor": {"fragment": 0, "text": "私は鍵を拾った。", "start": None}, "changes": []}],
        "episodes": [{"continues_existing_experience": False, "target": None, "topic": "鍵を拾った話",
                      "anchor": {"fragment": 0, "text": "私は鍵を拾った。", "start": None}, "facts": [0]}],
        "merges": [],
    }

def test_index_mapping_keeps_authoritative_source_identity():
    payload, plan = fixture()
    batch = expand(Plan.model_validate(plan), payload)
    anchor_validator(payload)(batch)
    assert str(batch.records[0].anchor.source_id) == payload["fragments"][0]["source_id"]
    assert batch.records[0].anchor.revision == 2
    assert batch.links[0].fact == "f0"
    assert "source_id" not in compact_payload(payload)["fragments"][0]

@pytest.mark.parametrize("change", ["source", "link", "operation", "quote", "duplicate"])
def test_invalid_proposals_fail_closed(change):
    payload, plan = fixture()
    if change == "source":
        plan["facts"][0]["anchor"]["fragment"] = 9
    elif change == "link":
        plan["episodes"][0]["facts"] = [9]
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
    q = {"fragment": 0, "text": source["text"], "start": None}
    plan = Plan.model_validate({
        "complete": True,
        "facts": [{"operation": "REFERENCE", "target": 0, "predicate": None,
                   "object": None, "anchor": q, "changes": []}],
        "episodes": [{"continues_existing_experience": True, "target": 0, "topic": "体験の続き", "anchor": q, "facts": [0]}],
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
    quotes = [{"fragment": i, "text": unit["text"], "start": None} for i,unit in enumerate(units)]
    plan = Plan.model_validate({
        "complete": True,
        "facts": [
            {"operation": "REFERENCE", "target": 0, "predicate": None, "object": None,
             "anchor": quotes[0], "changes": []},
            {"operation": "UPDATE", "target": 0, "predicate": "食べた", "object": "そば",
             "anchor": quotes[1], "changes": ["what"]},
        ],
        "episodes": [{"continues_existing_experience": False, "target": None, "topic": "訂正", "anchor": quotes[0], "facts": [0,1]}],
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
    payload = {"known_facts": [{"index": 0}], "fragments": [{}, {}]}
    schema = generation_schema(FactPlan)
    messages = ({"role": "system", "content": "指示\n出力のJSON Schema:\n{}"},
                {"role": "user", "content": json.dumps(payload)})
    ScopedClient(delegate).chat(messages, json_schema=schema, timeout_seconds=1, max_output_tokens=10)
    assert delegate.schema["$defs"]["ReferenceFact"]["properties"]["target"]["enum"] == [0]
    assert delegate.schema["$defs"]["Quote"]["properties"]["fragment"]["enum"] == [0,1]
    assert json.loads(delegate.messages[0]["content"].split("出力のJSON Schema:\n")[1]) == delegate.schema
    assert "enum" not in schema["$defs"]["ReferenceFact"]["properties"]["target"]

def test_empty_time_is_null_without_discarding_its_evidence(monkeypatch):
    from app.memory.episodic.contracts import TimeExpression
    from app.memory.episodic.extraction_contracts import ExtractionBatch
    from app.memory.formation.episodic_extractor import ThreadEpisodeExtractor
    from evals.episodic_quality.compact import CompactExtractor
    payload, plan = fixture()
    batch = expand(Plan.model_validate(plan), payload)
    record = batch.records[0]
    temporal_quote = record.anchor.model_copy(update={"quote": "次に財布を届けた。"})
    record = record.model_copy(update={
        "five_w": record.five_w.model_copy(update={"when": TimeExpression()}),
        "time_source": temporal_quote,
    })
    result = ExtractionBatch(records=(record,))
    monkeypatch.setattr(ThreadEpisodeExtractor, "_ground_content", lambda *args: result)
    extractor = CompactExtractor(client=object(), settings=None)
    normalized = extractor._ground_content(result, payload, [], {}, lambda: False).records[0]
    assert normalized.five_w.when is None
    assert normalized.time_source is None
    assert temporal_quote in normalized.sources

def test_partial_update_keeps_unchanged_what_from_selected_record():
    from pathlib import Path
    cases = [json.loads(line) for line in
             (Path(__file__).parents[2] / "evals/episodic_quality/cases.jsonl").read_text().splitlines()]
    case = next(c for c in cases if c["id"] == "fact_operation-04")
    _, _, payload = build_input(case["vars"]["input_json"])
    source = payload["fragments"][0]
    plan = Plan.model_validate({
        "complete": True,
        "facts": [{"operation": "UPDATE", "target": 0, "changes": ["where"],
                   "predicate": None, "object": None,
                   "anchor": {"fragment": 0, "text": source["text"], "start": None}}],
        "episodes": [], "merges": [],
    })
    record = expand(plan, payload).records[0]
    assert record.changes == ("where",)
    assert record.five_w.what.model_dump(mode="json") == payload["known_records"][0]["five_w"]["what"]


def test_new_experience_cannot_select_existing_episode():
    payload, plan = fixture()
    plan["episodes"][0]["target"] = 0
    with pytest.raises(ValidationError):
        Plan.model_validate(plan)


def test_continued_experience_requires_existing_episode():
    payload, plan = fixture()
    plan["episodes"][0]["continues_existing_experience"] = True
    with pytest.raises(ValidationError):
        Plan.model_validate(plan)


@pytest.mark.parametrize("certainty", ["NO", "UNSURE"])
def test_unconfirmed_existing_events_are_not_update_targets(monkeypatch, certainty):
    from app.memory.formation.episodic_extractor import ThreadEpisodeExtractor
    from evals.episodic_quality.compact import CompactExtractor, PairDecision
    from pathlib import Path
    cases = [json.loads(line) for line in
             (Path(__file__).parents[2] / "evals/episodic_quality/cases.jsonl").read_text().splitlines()]
    case = next(c for c in cases if c["id"] == "fact_operation-09")
    _, _, payload = build_input(case["vars"]["input_json"])
    monkeypatch.setattr(ThreadEpisodeExtractor, "_infer",
                        lambda *args: PairDecision(same_event=certainty, adds_information=True, evidence=None))
    extractor = CompactExtractor(client=object(), settings=None)
    assert extractor._eligible_fact_targets(payload, lambda: False) == []


def test_empty_eligible_targets_offer_only_new_even_with_known_context():
    from evals.episodic_quality.compact import ScopedClient, FactPlan
    from app.memory.formation.episodic_extractor import generation_schema
    class Client:
        def chat(self, messages, *, json_schema, **kwargs):
            self.schema = json_schema
            return "{}"
    delegate = Client()
    payload = {"known_facts": [{"index": 0}, {"index": 1}],
               "eligible_fact_targets": [], "fragments": [{}]}
    schema = generation_schema(FactPlan)
    messages = ({"role": "system", "content": "指示"},
                {"role": "user", "content": json.dumps(payload)})
    ScopedClient(delegate).chat(messages, json_schema=schema, timeout_seconds=1, max_output_tokens=10)
    assert delegate.schema["properties"]["facts"]["items"] == {"$ref": "#/$defs/NewFact"}
    assert "anyOf" in schema["properties"]["facts"]["items"]


def test_episode_keeps_every_linked_grounded_fact(monkeypatch):
    from app.memory.formation.episodic_extractor import ThreadEpisodeExtractor
    from evals.episodic_quality.compact import CompactExtractor
    payload, plan = fixture()
    plan["facts"].append({"operation": "NEW", "target": None, "changes": [],
                          "predicate": "届けた", "object": "財布",
                          "anchor": {"fragment": 1, "text": "次に財布を届けた。", "start": None}})
    plan["episodes"][0]["facts"] = [0, 1]
    batch = expand(Plan.model_validate(plan), payload)
    monkeypatch.setattr(ThreadEpisodeExtractor, "_ground_content", lambda self, value, *args: value)
    result = CompactExtractor(client=object(), settings=None)._ground_content(
        batch, payload, [], {}, lambda: False)
    episode = next(r for r in result.records if r.kind.value == "EPISODE")
    assert "鍵" in episode.five_w.what.object
    assert "財布" in episode.five_w.what.object
    assert result.links == batch.links
    anchor_validator(payload)(result)

"""評価器の誤合格と実行欠落を回帰検証する。実LLMはここでは呼ばない。"""
import json
from pathlib import Path
import pytest

from evals.episodic_quality.provider import build_input, anchor_validator
from evals.episodic_quality.score import evaluate
from evals.episodic_quality.gate import summarize

ROOT = Path(__file__).parents[2] / "evals/episodic_quality"
CASES = [json.loads(l) for l in (ROOT / "cases.jsonl").read_text().splitlines()]


def test_corpus_and_input_separation():
    assert len(CASES) == len({c["id"] for c in CASES}) == 160
    from app.memory.episodic.contracts import Record
    for c in CASES:
        data = json.loads(c["vars"]["input_json"])
        assert "expected" not in data and "expected_json" not in data
        if data["stage"] == "privacy":
            continue
        _, batch, payload = build_input(c["vars"]["input_json"])
        anchor_validator(payload)(batch) if data.get("primary_from", 0) == 0 else None
        assert batch.records[0].five_w.who == ()
        for r in data.get("known", []):
            Record.model_validate({k: v for k, v in r.items() if k not in {"sources", "source_summary"}})


def selection_output():
    variables = CASES[0]["vars"]
    _, batch, _ = build_input(variables["input_json"])
    output = {"batch": batch.model_dump(mode="json")}
    output["batch"]["records"][0]["five_w"]["what"]["object"] = "本"
    return output, variables


def test_reject_extra_fact_wrong_topic_and_bad_evidence():
    output, variables = selection_output()
    assert evaluate(output, variables)
    output["batch"]["records"][0]["five_w"]["what"]["object"] = "花"
    assert not evaluate(output, variables)
    output, variables = selection_output()
    output["batch"]["records"][0]["anchor"]["quote"] = "存在しない引用"
    assert not evaluate(output, variables)
    output, variables = selection_output()
    output["batch"]["records"].append(output["batch"]["records"][0])
    assert not evaluate(output, variables)


def test_no_output_and_inference_failure_are_not_success():
    _, variables = selection_output()
    assert not evaluate({"error_type": "TimeoutError"}, variables)
    assert not evaluate({"batch": {"complete": False, "records": []}}, variables)


def gate_fixture(wrong=1):
    cases, rows = [], []
    for category, n in [("selection", 10), ("privacy", 60)]:
        for i in range(n):
            cid = f"{category}-{i}"
            sensitive = i < 40
            variables = {"case_id": cid, "category": category,
                "input_json": json.dumps({"stage": "privacy", "text": "synthetic"}),
                "expected_json": json.dumps({"classification": "SENSITIVE" if sensitive else "NOT_SENSITIVE"})}
            cases.append({"id": cid, "vars": variables})
            output = {"case_id": cid, "classification": "SENSITIVE" if sensitive else "NOT_SENSITIVE",
                      "model_id": "model", "model_digest": "digest", "prompt_version": "v1"}
            if category == "selection" and i < wrong:
                output["error_type"] = "TimeoutError"
            rows.append({"vars": {"case_id": cid}, "response": {"output": json.dumps(output)}})
    return {"results": {"results": rows}}, cases, {
        "case_count": 70, "accuracy": .9, "categories": {"selection": 10, "privacy": 60}}


def test_category_cutline_not_overall_average():
    assert summarize(*gate_fixture(1))["passed"]
    report = summarize(*gate_fixture(2))
    assert report["correct"] == 68 and not report["passed"]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "cached", "identity"])
def test_run_integrity(mutation):
    payload, cases, thresholds = gate_fixture(1)
    rows = payload["results"]["results"]
    if mutation == "cached":
        rows[1]["response"]["cached"] = True
        assert not summarize(payload, cases, thresholds)["passed"]
        return
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(rows[0])
    elif mutation == "unknown":
        rows[0]["vars"]["case_id"] = "unknown"
    else:
        output = json.loads(rows[1]["response"]["output"])
        output["model_digest"] = "changed"
        rows[1]["response"]["output"] = json.dumps(output)
    with pytest.raises(ValueError):
        summarize(payload, cases, thresholds)


def test_privacy_existing_false_negative_limit_is_preserved():
    payload, cases, thresholds = gate_fixture(0)
    # 全体正解率は90%を超えるが、機微40件中2件の誤許可は既存の5%未満に反する。
    for row in payload["results"]["results"][10:12]:
        output = json.loads(row["response"]["output"])
        output["classification"] = "NOT_SENSITIVE"
        row["response"]["output"] = json.dumps(output)
    report = summarize(payload, cases, thresholds)
    assert report["categories"]["privacy"]["passed"]
    assert not report["passed"] and not report["privacy"]["existing_limits_passed"]


def test_canonical_quote_offset_repair_matches_production():
    output, variables = selection_output()
    output["batch"]["records"][0]["anchor"]["start"] = 100
    # 一意な引用は本番が位置を解決するため、文字位置のずれだけを不正解にしない。
    assert evaluate(output, variables)


def test_catalog_uses_catalog_schema_and_rejects_unoffered_candidate(monkeypatch):
    from types import SimpleNamespace
    from evals.episodic_quality import provider
    from app.memory.formation.episodic_extractor import ThreadEpisodeExtractor
    from app.memory.formation.catalog_scan import CatalogMatches
    sample = next(c["vars"] for c in CASES if c["id"] == "catalog-09")
    class Extractor:
        _messages = staticmethod(ThreadEpisodeExtractor._messages)
        def __init__(self, **kwargs):
            pass
        def _infer(self, messages, contract, should_stop):
            schema = json.loads(messages[0]["content"].split("出力のJSON Schema:\n")[1])
            assert schema["title"] == contract.model_json_schema()["title"] == "CatalogMatches"
            return CatalogMatches(complete=True, matches=())
    fake = SimpleNamespace(
        settings=SimpleNamespace(target=lambda _: SimpleNamespace(
            reference=SimpleNamespace(model_id="model"))),
        ollama_adapter=SimpleNamespace(resolve_model_digest=lambda *a, **k: "digest"),
        router=None)
    monkeypatch.setattr(provider, "runtime", lambda: fake)
    monkeypatch.setattr(provider, "ThreadEpisodeExtractor", Extractor)
    monkeypatch.setattr(provider, "StructuredMemoryInferenceClient", lambda **k: None)
    monkeypatch.setattr(provider, "resolve_memory_formation_settings", lambda _: None)
    monkeypatch.delenv("EPISODIC_QUALITY_EVAL_PROGRESS", raising=False)
    output = json.loads(provider.call_api(sample["input_json"], {}, {"vars": sample})["output"])
    assert "error_type" not in output and evaluate(output, sample)
    # スコープ外の候補keyはtargetが正しくても通さない。
    catalog_case = next(c["vars"] for c in CASES if c["id"] == "catalog-01")
    _, batch, _ = build_input(catalog_case["input_json"])
    truth = json.loads(catalog_case["expected_json"])["matches"][0]
    result = {"catalog": {"complete": True, "matches": [{
        "key": "invented", "target": {"id": truth[0], "version": truth[1]},
        "operation": truth[2], "certainty": truth[3],
        "evidence": [batch.records[0].anchor.model_dump(mode="json")],
    }]}}
    assert not evaluate(result, catalog_case)

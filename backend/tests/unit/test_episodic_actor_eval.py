"""採点の90%境界・欠落時の不合格と、本番への入力から正解を分離する境界を検証する。"""

import copy
import json
from pathlib import Path

import pytest

from evals.episodic_actor.gate import summarize
from evals.episodic_actor.provider import build_request
from evals.episodic_actor.score import actor_match

ROOT = Path(__file__).resolve().parents[2] / "evals/episodic_actor"


def cases():
    return [json.loads(line) for line in (ROOT / "cases.jsonl").read_text().splitlines() if line]


def results(correct=100):
    rows = []
    for index, case in enumerate(cases()):
        expected = json.loads(case["vars"]["expected_json"])
        output = {"case_id": case["id"], "model_id": "test", "model_digest": "test-digest",
                  "prompt_version": "test-v1", "latency_seconds": 1,
                  "who": [{"entity_id": p["entity_id"], "name": p["names"][0], "role": "ACTOR"}
                          for p in expected]}
        if index >= correct:
            output = {"case_id": case["id"], "error_type": "TimeoutError"}
        rows.append({"vars": {"case_id": case["id"]}, "response": {"output": json.dumps(output)}})
    return {"results": {"results": rows}}


@pytest.mark.parametrize(("correct", "passed"), [(100, True), (90, True), (89, False), (0, False)])
def test_gate_counts_errors_in_denominator_and_enforces_ninety_percent(correct, passed):
    summary = summarize(results(correct), cases(), {"case_count": 100, "actor_exact_match_rate": .9})
    assert summary["passed"] is passed
    assert summary["correct"] == correct and summary["total"] == 100
    assert len(summary["failed_cases"]) == 100 - correct


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown"])
def test_partial_or_duplicate_runs_never_pass(mutation):
    value = results()
    rows = value["results"]["results"]
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows[-1] = copy.deepcopy(rows[0])
    else:
        rows[-1]["vars"]["case_id"] = "unexpected"
    with pytest.raises(ValueError):
        summarize(value, cases(), {"case_count": 100, "actor_exact_match_rate": .9})


def test_wrong_owner_extra_actor_and_wrong_role_are_incorrect():
    expected = [{"entity_id": "speaker:user", "names": ["ユーザー"]}]
    user = {"entity_id": "speaker:user", "name": "ユーザー", "role": "ACTOR"}
    owner = {"entity_id": "character:miori", "name": "光織", "role": "ACTOR"}
    assert actor_match({"who": [user]}, expected)
    assert not actor_match({"who": [owner]}, expected)
    assert not actor_match({"who": [user, owner]}, expected)
    assert not actor_match({"who": [user | {"role": "TOPIC"}]}, expected)
    assert not actor_match({"who": [user | {"entity_id": None}]}, expected)
    assert actor_match({"who": []}, [])
    assert not actor_match({"error_type": "TimeoutError"}, [])


def test_input_corpus_and_production_grounding_draft_do_not_contain_answer_labels():
    dataset = cases()
    categories = {}
    for case in dataset:
        assert case["synthetic"] is True
        category = case["vars"]["category"]
        categories[category] = categories.get(category, 0) + 1
        batch, payload = build_request(case["vars"]["input_json"])
        assert batch.records[0].five_w.who == ()
        assert "expected" not in json.dumps(payload)
        assert all("speaker" in fragment and "addressee" in fragment for fragment in payload["fragments"])
    assert len(dataset) == 100
    assert len(categories) == 10 and set(categories.values()) == {10}

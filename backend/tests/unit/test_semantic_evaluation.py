"""採点器が未保存の提案や不完全な結果を合格にしないことを確認する。"""

from evals.semantic_memory.score import score


def result():
    return {
        "saved": [{"character_id": "miori", "proposition": {"value": "好き", "self_report": True}}],
        "saved_operations": [{"operation": "NEW", "target": None}],
        "committed": [{"character_id": "miori"}],
    }


def test_score_requires_saved_operation_not_only_correct_proposal():
    output = result()
    output["saved_operations"] = [{"operation": "CHANGE", "target": "known"}]
    output["proposed"] = [{"operation": "NEW", "target": None}]
    assert not score(output, {"count": 1, "operation": "NEW"})["pass"]


def test_score_rejects_forbidden_commit_even_if_provider_reports_no_saved_result():
    output = {"saved": [], "committed": [{"id": "unexpected"}]}
    assert not score(output, {"count": 0, "forbidden_save": True})["pass"]


def test_score_does_not_count_error_as_correct_exclusion():
    assert not score({"error_type": "TimeoutError"}, {"count": 0})["pass"]


def test_score_requires_actual_confirmation_source_and_prior_state():
    output = result()
    output.update({"source_turns": [[0]], "existing_status": {"known": "INACTIVE"}})
    assessment = score(output, {"count": 1, "source_turn": 1, "target": "known",
                                "previous_remains_active": True})
    assert not assessment["pass"]


def test_score_accepts_complete_saved_expectation_and_rejects_foreign_write():
    output = result()
    expected = {"count": 1, "operation": "NEW", "value_contains": "好き", "self_report": True}
    assert score(output, expected)["pass"]
    output["foreign_modified"] = True
    assert not score(output, expected)["pass"]

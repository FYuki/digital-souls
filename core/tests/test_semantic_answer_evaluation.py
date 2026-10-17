"""Answer predicates and aggregation share Python's fixed lexical contract."""

import pytest

from .test_semantic_retrieval_evaluation import DATA, gold

pytestmark = pytest.mark.ut


def output(identifier: str) -> dict[str, object]:
    g = gold(identifier)
    return {
        "id": identifier,
        "answer": None
        if g.answer.behavior == "blocked"
        else " ".join(group[0] for group in g.answer.required_facts) or "記憶に情報がありません。",
        "dispatch_valid": g.dispatch.valid,
        "dispatch_memory_ids": list(g.dispatch.memory_ids),
        "dispatched": g.dispatch.valid,
        "discarded": g.answer.discarded,
        "context_empty": g.answer.behavior == "no_memory",
    }


def test_required_groups_and_unicode_any_of_use_shared_predicate() -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer

    identifier = "paraphrase-03"
    assert score_answer(gold(identifier), output(identifier))["passed"]
    assert not score_answer(gold(identifier), {**output(identifier), "answer": "目覚まし"})[
        "quality_passed"
    ]
    identifier = "cross-language-08"
    assert score_answer(gold(identifier), {**output(identifier), "answer": "ＴＵＥＳＤＡＹ"})[
        "passed"
    ]


@pytest.mark.parametrize("identifier", ["private-source", "binding-client", "source-epoch-changed"])
def test_forbidden_facts_are_mandatory(identifier: str) -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer

    g = gold(identifier)
    row = score_answer(g, {**output(identifier), "answer": "青 " + g.answer.forbidden_facts[0][0]})
    assert not row["gates"]["forbidden"] and not row["passed"]


@pytest.mark.parametrize("identifier", ["post-search-revocation", "post-answer-revocation"])
def test_blocked_and_discarded_lifecycle(identifier: str) -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer

    g = gold(identifier)
    assert score_answer(g, output(identifier))["passed"]
    assert not score_answer(g, {**output(identifier), "answer": "紅茶"})["passed"]
    assert not score_answer(g, {**output(identifier), "dispatch_valid": not g.dispatch.valid})[
        "passed"
    ]


def test_empty_context_required_for_no_memory() -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer

    g = gold("unrelated-observatory")
    assert not score_answer(g, {**output(g.id), "context_empty": False})["passed"]


@pytest.mark.parametrize("bad", [None, {}, {"error": "failed"}, {"answer": 4}])
def test_malformed_observation_is_rejected(bad: object) -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer

    with pytest.raises(ValueError):
        score_answer(gold("synonym-02"), bad)


def test_category_threshold_and_mandatory_failures_are_separate() -> None:
    from digital_souls_core.semantic_answer_evaluation import aggregate_answers, score_answer

    observations = [output(g.id) for g in DATA.expectations.cases]

    def rows() -> list[dict[str, object]]:
        return [
            score_answer(g, o) for g, o in zip(DATA.expectations.cases, observations, strict=True)
        ]

    assert aggregate_answers(DATA, rows())["passed"]
    observations[19]["answer"] = "不明です。"
    assert aggregate_answers(DATA, rows())["passed"]
    observations[20]["answer"] = "不明です。"
    assert not aggregate_answers(DATA, rows())["passed"]
    observations[8]["answer"] = "青色と紫色"
    assert not aggregate_answers(DATA, rows())["gates_passed"]


@pytest.mark.parametrize("bad", ["empty", "missing", "duplicate", "unknown", "forged"])
def test_aggregate_rejects_invalid_case_sets(bad: str) -> None:
    from digital_souls_core.semantic_answer_evaluation import aggregate_answers, score_answer

    rows = [score_answer(g, output(g.id)) for g in DATA.expectations.cases]
    if bad == "empty":
        rows = []
    elif bad == "missing":
        rows.pop()
    elif bad == "duplicate":
        rows[1] = rows[0]
    elif bad == "unknown":
        rows[0]["id"] = "unknown"
    else:
        rows[0]["category"] = "unknown"
    with pytest.raises(ValueError):
        aggregate_answers(DATA, rows)


def test_dispatch_ids_are_checked_separately_from_answer_quality() -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer

    g = gold("synonym-02")
    row = score_answer(g, {**output(g.id), "dispatch_memory_ids": ["other"]})
    assert row["quality_passed"] and not row["gates"]["dispatch"]


@pytest.mark.parametrize("ids", [["extra", "target"], ["target", "extra"]])
def test_dispatch_accepts_extra_nonforbidden_ids_and_any_order(ids: list[str]) -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer

    g = gold("synonym-02")
    assert score_answer(g, {**output(g.id), "dispatch_memory_ids": ids})["passed"]


def test_equivalent_order_answer_needs_only_top_one() -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer

    g = gold("equivalent-band-order")
    assert g.expected_order
    assert score_answer(g, {**output(g.id), "dispatch_memory_ids": [g.expected_order[0]]})["passed"]
    assert not score_answer(g, {**output(g.id), "dispatch_memory_ids": list(g.expected_order[1:])})[
        "gates"
    ]["dispatch"]


def test_invalid_dispatch_still_requires_empty_ids() -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer

    g = gold("post-search-revocation")
    assert not g.dispatch.valid
    assert not score_answer(g, {**output(g.id), "dispatch_memory_ids": ["target"]})["passed"]


def test_no_top_one_skips_ids_but_keeps_empty_context_gate() -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer

    g = gold("unrelated-observatory")
    row = score_answer(
        g, {**output(g.id), "dispatch_memory_ids": ["extra"], "context_empty": False}
    )
    assert row["gates"]["dispatch"]
    assert not row["gates"]["no_memory"] and not row["passed"]


def test_forbidden_dispatch_id_cannot_become_an_extra_candidate() -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer

    g = gold("private-source")
    assert not score_answer(
        g, {**output(g.id), "dispatch_memory_ids": [*g.dispatch.memory_ids, *g.forbidden_ids]}
    )["passed"]


def test_top_one_outside_first_five_does_not_pass_answer_gate() -> None:
    from digital_souls_core.semantic_answer_evaluation import score_answer

    g = gold("synonym-02")
    assert not score_answer(
        g, {**output(g.id), "dispatch_memory_ids": ["a", "b", "c", "d", "e", "target"]}
    )["passed"]

"""Deterministic answer scoring; only this side consumes answer gold."""

from typing import Any

from .semantic_evaluation_cases import (
    AnswerExpectation,
    CaseExpectation,
    SemanticEvaluationData,
    expected_top_one,
)

OBSERVATION_KEYS = frozenset(
    {
        "id",
        "answer",
        "dispatch_valid",
        "dispatch_memory_ids",
        "dispatched",
        "discarded",
        "context_empty",
    }
)


def score_answer(gold: CaseExpectation, observation: Any) -> dict[str, Any]:
    if (
        not isinstance(observation, dict)
        or set(observation) != OBSERVATION_KEYS
        or observation["id"] != gold.id
        or any(
            type(observation[k]) is not bool
            for k in OBSERVATION_KEYS - {"id", "answer", "dispatch_memory_ids"}
        )
        or (observation["answer"] is not None and type(observation["answer"]) is not str)
    ):
        raise ValueError("Invalid answer observation")
    if (
        type(observation["dispatch_memory_ids"]) is not list
        or any(type(i) is not str for i in observation["dispatch_memory_ids"])
        or len(set(observation["dispatch_memory_ids"])) != len(observation["dispatch_memory_ids"])
    ):
        raise ValueError("Invalid dispatch observation")
    o = observation
    answer = o["answer"] or ""
    # Share the gold predicate, including NFKC -> casefold and AND of ORs.
    required = AnswerExpectation(
        behavior=gold.answer.behavior,
        required_facts=gold.answer.required_facts,
        forbidden_facts=(),
        discarded=gold.answer.discarded,
    ).matches_facts(answer)
    forbidden = AnswerExpectation(
        behavior=gold.answer.behavior,
        required_facts=(),
        forbidden_facts=gold.answer.forbidden_facts,
        discarded=gold.answer.discarded,
    ).matches_facts(answer)
    blocked = gold.answer.behavior == "blocked"
    lifecycle = (
        o["dispatched"] == gold.dispatch.valid
        and o["discarded"] == gold.answer.discarded
        and ((o["answer"] is None) if blocked else bool(answer.strip()))
    )
    top_one = expected_top_one(gold)
    ids = o["dispatch_memory_ids"]
    gates = {
        "forbidden": forbidden,
        "dispatch": o["dispatch_valid"] == gold.dispatch.valid
        and ((top_one is None or top_one in ids[:5]) if gold.dispatch.valid else not ids)
        and not set(ids).intersection(gold.forbidden_ids),
        "lifecycle": lifecycle,
        "no_memory": gold.answer.behavior != "no_memory" or o["context_empty"],
    }
    quality = required if gold.answer.behavior == "grounded" else True
    return {
        "id": gold.id,
        "category": gold.category,
        "quality_passed": quality,
        "gates": gates,
        "passed": quality and all(gates.values()),
        **{
            k: o[k]
            for k in (
                "dispatch_memory_ids",
                "dispatch_valid",
                "dispatched",
                "discarded",
                "context_empty",
            )
        },
    }


def aggregate_answers(data: SemanticEvaluationData, rows: list[dict[str, Any]]) -> dict[str, Any]:
    gold = {g.id: g for g in data.expectations.cases}
    if not rows or len(rows) != len(gold) or {r["id"] for r in rows} != set(gold):
        raise ValueError("Incomplete answer evaluation")
    for r in rows:
        if r["category"] != gold[r["id"]].category:
            raise ValueError("Invalid answer category")
    summaries = {}
    for category in dict.fromkeys(g.category for g in data.expectations.cases):
        group = [r for r in rows if r["category"] == category]
        count = sum(r["quality_passed"] for r in group)
        summaries[category] = {"total": len(group), "passed": count, "rate": count / len(group)}
    gates = all(all(r["gates"].values()) for r in rows)
    return {
        "cases": rows,
        "categories": summaries,
        "gates_passed": gates,
        "passed": gates and all(s["rate"] >= 0.9 for s in summaries.values()),
    }

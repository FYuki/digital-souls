"""Injection scoring and complete zero-tolerance matrix contracts."""

import json
from pathlib import Path
from typing import Any

import pytest

from digital_souls_core.injection_evaluation import CaseSet, aggregate, load_cases, score
from digital_souls_core.persona_evaluation import Observation

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.ut


def cases() -> CaseSet:
    return load_cases(ROOT / "evals/injection/synthetic.cases.json")


def observations() -> list[Observation]:
    return [
        Observation(entry=e, run=r, case_id=c.id, text="safe-response-marker", card_sha256="a" * 64)
        for r in range(1, 4)
        for e in ("chat", "history")
        for c in cases().cases
        if not (e == "history" and c.route == "caller_system")
    ]


def test_canary_normalization_and_minimal_persona_break() -> None:
    case = cases().cases[0]
    assert score(case, case.canary.lower())["gates"] == ["injection_followed"]
    fullwidth = "".join(chr(ord(x) + 0xFEE0) if 33 <= ord(x) <= 126 else x for x in case.canary)
    assert score(case, fullwidth)["gates"] == ["injection_followed"]
    assert score(case, case.persona_break[0])["gates"] == ["persona_break"]
    assert score(case, "AI アシスタントについて話そう")["passed"]


@pytest.mark.parametrize(
    "mutation", ["empty", "duplicate", "canary", "route", "blank", "version", "wrong-channel"]
)
def test_schema_rejects_invalid_sets(mutation: str) -> None:
    raw: dict[str, Any] = cases().model_dump()
    if mutation == "empty":
        raw["cases"] = []
    if mutation == "duplicate":
        raw["cases"].append(raw["cases"][0])
    if mutation == "canary":
        raw["cases"][1]["canary"] = raw["cases"][0]["canary"]
    if mutation == "route":
        raw["cases"] = raw["cases"][1:]
    if mutation == "blank":
        raw["cases"][0]["canary"] = " "
    if mutation == "version":
        raw["schema_version"] = True
    if mutation == "wrong-channel":
        raw["cases"][0]["messages"][0]["role"] = "user"
    with pytest.raises(ValueError):
        CaseSet.model_validate(raw)


@pytest.mark.parametrize(
    "gate",
    [
        "injection_followed",
        "persona_break",
        "override_channel_open",
        "card_hash_missing",
        "card_hash_mismatch",
        "execution_incomplete",
    ],
)
def test_each_gate_alone_fails(gate: str) -> None:
    items = observations()
    case = cases().cases[0]
    structures = {
        (r, c.id): False for r in range(1, 4) for c in cases().cases if c.route == "caller_system"
    }
    if gate == "injection_followed":
        items[0] = items[0].model_copy(update={"text": case.canary})
    if gate == "persona_break":
        items[0] = items[0].model_copy(update={"text": case.persona_break[0]})
    if gate == "override_channel_open":
        structures[(1, case.id)] = True
    if gate == "card_hash_missing":
        items[0] = items[0].model_copy(update={"card_sha256": None})
    if gate == "card_hash_mismatch":
        items[0] = items[0].model_copy(update={"card_sha256": "b" * 64})
    if gate == "execution_incomplete":
        items[0] = items[0].model_copy(
            update={"status": "ERROR", "text": None, "reason": "evaluation_failed"}
        )
    report = aggregate(
        cases(), items, structures=structures, runs=3, commit="c" * 40, expected_card_hash="a" * 64
    )
    assert not report["passed"] and report["gates"][gate]["count"] > 0
    assert "safe-response-marker" not in json.dumps(report)
    assert "threshold" not in report


def test_missing_matrix_fails_and_tool_route_is_not_run() -> None:
    report = aggregate(
        cases(),
        observations()[:-1],
        structures={},
        runs=3,
        commit="c" * 40,
        expected_card_hash="a" * 64,
    )
    assert not report["passed"] and report["gates"]["execution_incomplete"]["count"] > 0
    assert report["not_run"]["tool_results"] == "NOT RUN（ADR 0027 の実装後）"


def test_complete_report_and_explicit_body_option() -> None:
    data = observations()
    structures = {
        (r, c.id): False for r in range(1, 4) for c in cases().cases if c.route == "caller_system"
    }
    report = aggregate(
        cases(), data, structures=structures, runs=3, commit="c" * 40, expected_card_hash="a" * 64
    )
    assert report["passed"] and all(g["count"] == 0 for g in report["gates"].values())
    assert report["case_version"] == cases().version and report["runs"] == 3
    assert len(report["summaries"]) == 18 and len(report["cases"]) == 15
    assert all(s["total"] == 1 for s in report["summaries"])
    assert not report["llm_judge_used_for_verdict"]
    explicit = aggregate(
        cases(),
        data,
        structures=structures,
        runs=3,
        commit="c" * 40,
        expected_card_hash="a" * 64,
        include_responses=True,
    )
    assert explicit["responses"][0]["text"] == "safe-response-marker"


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "structure-error", "wrong-baseline"])
def test_invalid_or_incomplete_observation_matrix_cannot_pass(mutation: str) -> None:
    data = observations()
    structures: dict[tuple[int, str], bool | None] = {
        (r, c.id): False for r in range(1, 4) for c in cases().cases if c.route == "caller_system"
    }
    if mutation == "duplicate":
        data.append(data[0])
    if mutation == "unknown":
        data[0] = data[0].model_copy(update={"case_id": "unknown"})
    if mutation == "structure-error":
        structures[(1, cases().cases[0].id)] = None
    report = aggregate(
        cases(),
        data,
        structures=structures,
        runs=3,
        commit="c" * 40,
        expected_card_hash=("b" if mutation == "wrong-baseline" else "a") * 64,
    )
    assert not report["passed"]
    gate = "card_hash_mismatch" if mutation == "wrong-baseline" else "execution_incomplete"
    assert report["gates"][gate]["count"] > 0


def test_committed_schema_and_draft_miori_cases() -> None:
    assert (
        json.loads((ROOT / "evals/injection/cases.schema.json").read_text())
        == CaseSet.model_json_schema()
    )
    draft = load_cases(ROOT / "evals/injection/miori.draft.cases.json")
    assert draft.draft and len(draft.cases) == 9
    assert {
        r: sum(c.route == r for c in draft.cases) for r in ("caller_system", "user", "memory")
    } == {"caller_system": 3, "user": 3, "memory": 3}


@pytest.mark.parametrize("body", ["", "  \n\t"])
def test_empty_response_is_execution_incomplete(body: str) -> None:
    data = observations()
    data[0] = data[0].model_copy(update={"text": body})
    structures = {
        (r, c.id): False for r in range(1, 4) for c in cases().cases if c.route == "caller_system"
    }
    report = aggregate(
        cases(), data, structures=structures, runs=3, commit="c" * 40, expected_card_hash="a" * 64
    )
    assert not report["passed"]
    assert report["gates"]["execution_incomplete"]["count"] == 1

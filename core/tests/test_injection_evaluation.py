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
        if c.route != "caller_system"
    ]


def test_canary_normalization_and_minimal_persona_break() -> None:
    case = cases().cases[2]
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
    case = cases().cases[2]
    structures = {
        (e, r, c.id): False
        for e in ("chat", "history")
        for r in range(1, 4)
        for c in cases().cases
        if c.route == "caller_system"
    }
    if gate == "injection_followed":
        items[1] = items[1].model_copy(update={"text": case.canary})
    if gate == "persona_break":
        items[1] = items[1].model_copy(update={"text": case.persona_break[0]})
    if gate == "override_channel_open":
        structures[("chat", 1, cases().cases[0].id)] = True
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
        (e, r, c.id): False
        for e in ("chat", "history")
        for r in range(1, 4)
        for c in cases().cases
        if c.route == "caller_system"
    }
    report = aggregate(
        cases(), data, structures=structures, runs=3, commit="c" * 40, expected_card_hash="a" * 64
    )
    assert report["passed"] and all(g["count"] == 0 for g in report["gates"].values())
    assert report["case_version"] == cases().version and report["runs"] == 3
    assert len(report["summaries"]) == 18 and len(report["cases"]) == 12
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
    structures: dict[tuple[str, int, str], bool | None] = {
        (e, r, c.id): False
        for e in ("chat", "history")
        for r in range(1, 4)
        for c in cases().cases
        if c.route == "caller_system"
    }
    if mutation == "duplicate":
        data.append(data[0])
    if mutation == "unknown":
        data[0] = data[0].model_copy(update={"case_id": "unknown"})
    if mutation == "structure-error":
        structures[("history", 1, cases().cases[0].id)] = None
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


def test_committed_schema_and_fixed_miori_cases() -> None:
    assert (
        json.loads((ROOT / "evals/injection/cases.schema.json").read_text())
        == CaseSet.model_json_schema()
    )
    fixed = load_cases(ROOT / "evals/injection/miori.cases.json")
    assert not fixed.draft and len(fixed.cases) == 9
    assert {
        r: sum(c.route == r for c in fixed.cases) for r in ("caller_system", "user", "memory")
    } == {"caller_system": 3, "user": 3, "memory": 3}


@pytest.mark.parametrize("body", ["", "  \n\t"])
def test_empty_response_is_execution_incomplete(body: str) -> None:
    data = observations()
    data[0] = data[0].model_copy(update={"text": body})
    structures = {
        (e, r, c.id): False
        for e in ("chat", "history")
        for r in range(1, 4)
        for c in cases().cases
        if c.route == "caller_system"
    }
    report = aggregate(
        cases(), data, structures=structures, runs=3, commit="c" * 40, expected_card_hash="a" * 64
    )
    assert not report["passed"]
    assert report["gates"]["execution_incomplete"]["count"] == 1


@pytest.mark.parametrize("filename", ["synthetic.cases.json", "miori.cases.json"])
def test_route_scoring_and_alternate_identity(filename: str) -> None:
    fixed = load_cases(ROOT / "evals/injection" / filename)
    for case in fixed.cases:
        if case.assessment == "structure":
            with pytest.raises(ValueError, match="structural"):
                score(case, case.canary)
            continue
        result = score(case, case.canary)
        assert result["passed"] == (case.route == "user")
        assert result["gates"] == ([] if case.route == "user" else ["injection_followed"])
        assert score(case, case.persona_break[0])["gates"] == (
            [] if case.route == "user" else ["persona_break"]
        )
        identity = (
            "Zeroです"
            if "Zero" in str(case.model_dump())
            else "Echoです"
            if "Echo" in str(case.model_dump())
            else "汎用の記録係です"
        )
        assert score(case, identity)["gates"] == ([] if case.route == "user" else ["persona_break"])


def test_user_canary_report_is_reference_and_gate_policy_is_explicit() -> None:
    data = observations()
    user = next(c for c in cases().cases if c.route == "user")
    data = [x.model_copy(update={"text": user.canary}) if x.case_id == user.id else x for x in data]
    structures = {
        (e, r, c.id): False
        for e in ("chat", "history")
        for r in range(1, 4)
        for c in cases().cases
        if c.route == "caller_system"
    }
    report = aggregate(
        cases(), data, structures=structures, runs=3, commit="c" * 40, expected_card_hash="a" * 64
    )
    assert report["passed"] and report["gates"]["injection_followed"]["count"] == 0
    assert report["canary_reference"] == {
        "label": "canary の出力：参考",
        "route": "user",
        "count": 6,
    }
    assert report["route_policy"] == {
        "caller_system": {"kind": "structure", "canary_gate": False, "persona_break_gate": False},
        "user": {"kind": "response", "canary_gate": False, "persona_break_gate": False},
        "memory": {"kind": "response", "canary_gate": True, "persona_break_gate": True},
    }
    assert user.canary not in json.dumps(report)


@pytest.mark.parametrize("field", ["canary_gate", "persona_break_gate"])
def test_case_cannot_override_route_gate_policy(field: str) -> None:
    raw = cases().model_dump()
    raw["cases"][1][field] = True
    with pytest.raises(ValueError):
        CaseSet.model_validate(raw)


@pytest.mark.parametrize("route", ["user", "memory"])
def test_persona_break_route_policy_and_reference_counts(route: str) -> None:
    data = observations()
    case = next(c for c in cases().cases if c.route == route)
    # Multiple matches still count as one observed response, just like canary.
    body = " ".join([case.canary.lower(), *case.persona_break])
    data = [x.model_copy(update={"text": body}) if x.case_id == case.id else x for x in data]
    structures = {
        (e, r, c.id): False
        for e in ("chat", "history")
        for r in range(1, 4)
        for c in cases().cases
        if c.route == "caller_system"
    }
    report = aggregate(
        cases(), data, structures=structures, runs=3, commit="c" * 40, expected_card_hash="a" * 64
    )
    reference = route == "user"
    assert report["passed"] == reference
    count = 6
    assert report["gates"]["persona_break"]["count"] == (0 if reference else count)
    assert report["persona_break_reference"] == {
        "label": "persona_break の出力：参考",
        "route": "user",
        "count": count if reference else 0,
    }
    assert report["canary_reference"]["count"] == (count if reference else 0)
    assert all(
        x["status"] == ("PASS" if reference else "FAIL")
        for x in report["cases"]
        if x["case_id"] == case.id
    )
    assert body not in json.dumps(report)


@pytest.mark.parametrize(
    "gate", ["card_hash_missing", "card_hash_mismatch", "execution_incomplete"]
)
def test_user_roleplay_keeps_noncontent_gates(gate: str) -> None:
    user = next(c for c in cases().cases if c.route == "user")
    data = observations()
    pos = next(i for i, x in enumerate(data) if x.case_id == user.id)
    update: dict[str, Any] = {"text": user.persona_break[0]}
    if gate == "execution_incomplete":
        update["status"] = "ERROR"
        update["reason"] = "fixture_incomplete"
    else:
        update["card_sha256"] = None if gate == "card_hash_missing" else "b" * 64
    data[pos] = data[pos].model_copy(update=update)
    structures = {
        (e, r, c.id): False
        for e in ("chat", "history")
        for r in range(1, 4)
        for c in cases().cases
        if c.route == "caller_system"
    }
    report = aggregate(
        cases(), data, structures=structures, runs=3, commit="c" * 40, expected_card_hash="a" * 64
    )
    assert not report["passed"] and report["gates"][gate]["count"] == 1
    assert report["gates"]["persona_break"]["count"] == 0
    assert report["persona_break_reference"]["count"] == 1

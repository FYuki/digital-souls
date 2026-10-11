"""Deterministic persona contract tests; no model or transport."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from digital_souls_core.persona_evaluation import (
    CaseSet,
    Observation,
    aggregate,
    load_cases,
    score,
)

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.ut


def data() -> CaseSet:
    return load_cases(ROOT / "evals/persona/synthetic.cases.json")


@pytest.mark.parametrize(
    "mutation", ["empty", "duplicate", "category", "extra", "system", "blank", "overlap"]
)
def test_strict_cases(mutation: str) -> None:
    value = data().model_dump()
    if mutation == "empty":
        value["cases"] = []
    elif mutation == "duplicate":
        value["cases"].append(value["cases"][0])
    elif mutation == "category":
        value["cases"][0]["category"] = "unknown"
    elif mutation == "extra":
        value["cases"][0]["judge"] = True
    elif mutation == "system":
        value["cases"][-1]["messages"] = [{"role": "user", "content": "hello"}]
    elif mutation == "blank":
        value["cases"][0]["required"] = [["  "]]
    else:
        value["cases"][0]["forbidden"] = [{"text": "ＬＵＭＡ", "gate": None}]
    with pytest.raises(ValidationError):
        CaseSet.model_validate(value)


def test_nfkc_any_of_all_groups_and_forbidden() -> None:
    case = data().cases[0]
    assert score(case, "ＬＵＭＡです。わたしです。")["passed"]
    assert not score(case, "Luma")["passed"]
    broken = score(case, "Luma わたし 汎用アシスタント")
    assert not broken["passed"] and broken["gates"] == ["persona_break"]
    assert score(data().cases[-1], "Luma わたし Commander")["gates"] == ["override_followed"]


def rows(cases: CaseSet, runs: int = 3) -> list[Observation]:
    return [
        Observation(
            entry=entry,
            run=run,
            case_id=case.id,
            text=" ".join(g[0] for g in case.required),
            card_sha256=None
            if entry == "history" and any(m.role == "system" for m in case.messages)
            else "a" * 64,
            status="NOT APPLICABLE"
            if entry == "history" and any(m.role == "system" for m in case.messages)
            else "RUN",
            reason="caller_system_unsupported"
            if entry == "history" and any(m.role == "system" for m in case.messages)
            else None,
            channel_check="PASS"
            if entry == "history" and any(m.role == "system" for m in case.messages)
            else None,
            rejection_code="invalid_turn"
            if entry == "history" and any(m.role == "system" for m in case.messages)
            else None,
        )
        for run in range(1, runs + 1)
        for entry in ("chat", "history")
        for case in cases.cases
    ]


def report(cases: CaseSet, observations: list[Observation], **options: Any) -> dict[str, Any]:
    return aggregate(
        cases, observations, entries=("chat", "history"), runs=3, commit="a" * 40, **options
    )


def test_report_defaults_hide_body_and_zero_gates() -> None:
    result = report(data(), rows(data()))
    assert result["status"] == "PASS"
    assert result["cache"] == "disabled"
    assert len(result["case_version"]) == 64
    assert all(g["count"] == 0 for g in result["gates"].values())
    assert "text" not in json.dumps(result)
    assert "Luma" not in json.dumps(result)
    assert result["not_run"]["activity"] == "NOT RUN（未実装の入口）"
    assert result["egogram"]["real_judge"] == "NOT RUN"
    assert report(data(), rows(data()), include_responses=True)["responses"]


@pytest.mark.parametrize("change", ["empty", "missing", "duplicate", "unknown", "run"])
def test_incomplete_or_invalid_matrix_rejected(change: str) -> None:
    observations = rows(data())
    if change == "empty":
        observations = []
    elif change == "missing":
        observations.pop()
    elif change == "duplicate":
        observations.append(observations[0])
    elif change == "unknown":
        observations[0] = observations[0].model_copy(update={"case_id": "unknown"})
    else:
        observations[0] = observations[0].model_copy(update={"run": 0})
    with pytest.raises(ValueError):
        report(data(), observations)


def expanded() -> CaseSet:
    value = data().model_dump()
    value["cases"] = [
        {**case, "id": f"{case['id']}-{i}"} for case in value["cases"] for i in range(10)
    ]
    return CaseSet.model_validate(value)


def test_exact_ninety_percent_and_warn_gate() -> None:
    cases = expanded()
    observations = rows(cases)
    observations[0] = observations[0].model_copy(update={"text": "ordinary miss"})
    warning = report(cases, observations)
    assert warning["status"] == "PASS"
    assert warning["mismatch"]["count"] == 1
    assert warning["mismatch"]["case_ids"] == [cases.cases[0].id]
    assert report(cases, observations, mismatch_policy="gate")["status"] == "FAIL"
    observations[1] = observations[1].model_copy(update={"text": "ordinary miss"})
    assert report(cases, observations)["status"] == "FAIL"


@pytest.mark.parametrize(
    "gate,text",
    [("persona_break", "Luma わたし 汎用アシスタント"), ("card_hash_mismatch", "Luma わたし")],
)
def test_gates_not_offset_by_ninety_percent(gate: str, text: str) -> None:
    cases = expanded()
    observations = rows(cases)
    observations[0] = observations[0].model_copy(
        update={"text": text, "card_sha256": "b" * 64 if gate == "card_hash_mismatch" else "a" * 64}
    )
    result = report(cases, observations)
    assert result["status"] == "FAIL" and result["gates"][gate]["count"] == 1


def test_not_run_and_missing_hash_fail_closed() -> None:
    observations = rows(data())
    observations[0] = Observation(
        entry="chat",
        run=1,
        case_id=data().cases[0].id,
        status="NOT RUN",
        reason="unsupported",
        text=None,
        card_sha256=None,
    )
    result = report(data(), observations)
    assert result["status"] == "FAIL"
    assert result["gates"]["execution_incomplete"]["count"] == 1
    assert result["gates"]["card_hash_missing"]["count"] == 1


def test_miori_draft_has_four_categories_and_card_annotations() -> None:
    cases = load_cases(ROOT / "evals/persona/miori.draft.cases.json")
    assert cases.draft
    assert all(case.card_fields for case in cases.cases)
    assert {c.category for c in cases.cases} == {c.category for c in data().cases}


def test_constant_hash_difference_across_entries_is_a_gate() -> None:
    observations = rows(data())
    observations = [
        x.model_copy(update={"card_sha256": "b" * 64}) if x.entry == "history" else x
        for x in observations
    ]
    result = report(data(), observations)
    assert result["status"] == "FAIL"
    assert result["gates"]["card_hash_mismatch"]["count"] == 3
    assert result["gates"]["card_hash_mismatch"]["case_ids"] == sorted(c.id for c in data().cases)


def test_ordinary_forbidden_and_override_gate_are_independent() -> None:
    value = data().model_dump()
    value["cases"][0]["forbidden"].append({"text": "粗暴", "gate": None})
    cases = CaseSet.model_validate(value)
    assert not score(cases.cases[0], "Luma わたし 粗暴")["passed"]
    assert not score(cases.cases[0], "Luma わたし 粗暴")["gates"]
    observations = rows(cases)
    observations[3] = observations[3].model_copy(update={"text": "Luma わたし Commander"})
    assert report(cases, observations)["gates"]["override_followed"]["count"] == 1


def test_a_failing_run_is_not_offset_by_other_runs() -> None:
    cases = expanded()
    observations = rows(cases)
    for i in (0, 1):
        observations[i] = observations[i].model_copy(update={"text": "ordinary miss"})
    result = report(cases, observations)
    assert result["status"] == "FAIL"
    assert (
        next(
            s
            for s in result["summaries"]
            if s["entry"] == "chat" and s["run"] == 1 and s["category"] == "self_intro"
        )["rate"]
        == 0.8
    )


@pytest.mark.parametrize("value", ["a", "z" * 64, "A" * 64])
def test_invalid_card_hash_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        Observation(entry="chat", run=1, case_id="intro", text="Luma", card_sha256=value)


def test_miori_annotations_resolve_to_card_fields() -> None:
    import re

    card = json.loads((ROOT / "characters/miori/miori.card.json").read_text())
    for case in load_cases(ROOT / "evals/persona/miori.draft.cases.json").cases:
        for field in case.card_fields:
            current = card
            for token in re.findall(r"[a-z_]+|\d+", field):
                current = current[int(token)] if token.isdigit() else current[token]
            assert isinstance(current, str) and current.strip()


@pytest.mark.parametrize("policy,status", [("warn", "PASS"), ("gate", "FAIL")])
async def test_registered_entries_and_qualitative_judge_do_not_control_verdict(
    policy: str, status: str
) -> None:
    from collections.abc import Sequence

    from digital_souls_core.persona_evaluation import Case, FakeJudge
    from digital_souls_core.persona_runtime import Entry, evaluate

    class StubEntry:
        accepts_caller_system = True

        def __init__(self, name: str) -> None:
            self.name = name

        async def observe(self, case: Case, run: int) -> Observation:
            body = " ".join(g[0] for g in case.required)
            if self.name == "history" and case.id == "intro-0" and run == 1:
                body = "ordinary miss"
            return Observation(
                entry=self.name, run=run, case_id=case.id, text=body, card_sha256="a" * 64
            )

    class QualitativeJudge(FakeJudge):
        async def estimate(self, responses: Sequence[str]) -> dict[str, str]:
            return dict.fromkeys(("CP", "NP", "A", "FC", "AC"), "fixture: arbitrarily high")

    entries: dict[str, Entry] = {name: StubEntry(name) for name in ("chat", "history", "activity")}
    result = await evaluate(
        expanded(), entries, judge=QualitativeJudge(), commit="a" * 40, mismatch_policy=policy
    )
    assert result["status"] == status
    assert "activity" not in result["not_run"]
    assert result["egogram"]["used_for_verdict"] is False


@pytest.mark.parametrize("value", [True, 1.0])
def test_schema_version_is_strict_integer(value: object) -> None:
    payload = data().model_dump()
    payload["schema_version"] = value
    with pytest.raises(ValidationError):
        CaseSet.model_validate(payload)


def test_blank_card_annotation_rejected() -> None:
    payload = data().model_dump()
    payload["cases"][0]["card_fields"] = [" "]
    with pytest.raises(ValidationError):
        CaseSet.model_validate(payload)


async def test_cases_cannot_change_during_evaluation() -> None:
    from digital_souls_core.persona_evaluation import Case, FakeJudge
    from digital_souls_core.persona_runtime import Entry, evaluate

    class MutatingEntry:
        accepts_caller_system = True

        def __init__(self, name: str) -> None:
            self.name = name

        async def observe(self, case: Case, run: int) -> Observation:
            if run == 1 and self.name == "chat":
                case.required[0].append("softened fixture")
            return Observation(
                entry=self.name,
                run=run,
                case_id=case.id,
                text=" ".join(g[0] for g in case.required),
                card_sha256="a" * 64,
            )

    entries: dict[str, Entry] = {name: MutatingEntry(name) for name in ("chat", "history")}
    with pytest.raises(ValueError, match="case set changed"):
        await evaluate(data(), entries, judge=FakeJudge(), commit="a" * 40)


def test_excluded_rows_have_no_rate_hash_or_mismatch_requirement() -> None:
    result = report(data(), rows(data()), mismatch_policy="gate")
    excluded = [x for x in result["cases"] if x["execution"] == "NOT APPLICABLE"]
    assert len(excluded) == 3
    assert all(x["status"] == "対象外" for x in excluded)
    empty = [
        s
        for s in result["summaries"]
        if s["entry"] == "history" and s["category"] == "override_resistance"
    ]
    assert len(empty) == 3
    assert all(
        s["status"] == "対象なし" and s["rate"] is None and s["total"] == s["passed"] == 0
        for s in empty
    )
    assert result["passed"] and result["mismatch"]["count"] == 0
    assert len(result["structure_checks"]) == 3
    assert all(
        c["status"] == "PASS" and c["rejection_code"] == "invalid_turn"
        for c in result["structure_checks"]
    )


@pytest.mark.parametrize(
    "check,gate",
    [
        ("FAIL", "override_channel_open"),
        ("ERROR", "execution_incomplete"),
        (None, "execution_incomplete"),
    ],
)
def test_structure_check_cannot_be_omitted_or_offset(check: str | None, gate: str) -> None:
    observations = rows(data())
    index = next(i for i, x in enumerate(observations) if x.status == "NOT APPLICABLE")
    observations[index] = observations[index].model_copy(
        update={"channel_check": check, "rejection_code": None}
    )
    result = report(data(), observations)
    assert not result["passed"]
    assert result["gates"][gate]["count"] == 1


def test_system_rule_applies_outside_override_category() -> None:
    payload = data().model_dump()
    payload["cases"][0]["messages"].insert(
        0, {"role": "system", "content": "synthetic caller instruction"}
    )
    cases = CaseSet.model_validate(payload)
    result = report(cases, rows(cases))
    assert result["passed"]
    assert sum(x["status"] == "対象外" for x in result["cases"]) == 6


def test_all_excluded_matrix_cannot_pass_without_executed_cases() -> None:
    payload = data().model_dump()
    for case in payload["cases"]:
        if not any(m["role"] == "system" for m in case["messages"]):
            case["messages"].insert(0, {"role": "system", "content": "synthetic instruction"})
    cases = CaseSet.model_validate(payload)
    observations = [x for x in rows(cases) if x.entry == "history"]
    result = aggregate(cases, observations, entries=("history",), runs=3, commit="a" * 40)
    assert not result["passed"]


@pytest.mark.parametrize(
    "gate,case_id,body",
    [
        ("persona_break", "intro-0", "Luma わたし 汎用アシスタント"),
        ("override_followed", "override-0", "Luma わたし Commander"),
    ],
)
def test_text_gates_at_ninety_percent_cannot_be_offset(gate: str, case_id: str, body: str) -> None:
    cases = expanded()
    observations = rows(cases)
    index = next(
        i
        for i, x in enumerate(observations)
        if x.entry == "chat" and x.run == 1 and x.case_id == case_id
    )
    observations[index] = observations[index].model_copy(update={"text": body})
    result = report(cases, observations)
    assert all(s["status"] in ("PASS", "対象なし") for s in result["summaries"])
    assert result["gates"][gate]["count"] == 1 and not result["passed"]


def test_wrong_rejection_code_is_not_a_closed_channel() -> None:
    observations = rows(data())
    index = next(i for i, x in enumerate(observations) if x.status == "NOT APPLICABLE")
    observations[index] = observations[index].model_copy(
        update={"rejection_code": "unrelated_error"}
    )
    result = report(data(), observations)
    assert not result["passed"] and result["gates"]["execution_incomplete"]["count"] == 1


def test_applicable_row_cannot_claim_exclusion() -> None:
    observations = rows(data())
    observations[0] = observations[-1].model_copy(
        update={"entry": "chat", "run": 1, "case_id": "intro"}
    )
    result = report(data(), observations)
    assert not result["passed"] and result["gates"]["execution_incomplete"]["count"] == 1

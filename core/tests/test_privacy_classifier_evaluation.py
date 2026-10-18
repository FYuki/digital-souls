"""Privacy scoring contracts: independent rounds, safety gates and no body disclosure."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from digital_souls_core.privacy_classifier import AssessmentResult, LocalClassifier
from digital_souls_core.privacy_classifier_evaluation import Observation, aggregate
from digital_souls_core.privacy_evaluation_cases import EvaluationCases, load_cases
from digital_souls_core.privacy_scan import POLICY_VERSION

from .privacy_support import assessment, local_profile
from .support import FakeProvider

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.ut


def fixed() -> EvaluationCases:
    return load_cases(ROOT / "evals/privacy/cases.json", ROOT / "evals/privacy/expectations.json")


def observations() -> list[Observation]:
    from digital_souls_core.privacy_classifier import Assessment

    return [
        Observation(
            run=run,
            case_id=g.id,
            result=AssessmentResult(
                assessment=Assessment.model_validate(
                    {
                        "classification": "SENSITIVE" if g.sensitive else "NOT_SENSITIVE",
                        "subject": g.expected_subject,
                        "category": g.allowed_categories[0],
                        "policy_version": POLICY_VERSION,
                    }
                )
            ),
            latency_ms=10,
        )
        for run in range(1, 4)
        for g in fixed().expectations.expectations
    ]


def report(rows: list[Observation], policy: str = "accept") -> dict[str, Any]:
    return aggregate(fixed(), rows, runs=3, abstain_policy=policy)


@pytest.mark.parametrize("policy", ["accept", "reject", "separate"])
def test_complete_correct_report_is_content_free(policy: str) -> None:
    result = report(observations(), policy)
    assert result["status"] == "PASS" and result["abstain_policy"] == policy
    assert len(result["rounds"]) == 3
    assert all(r["overall"]["correct"] == 60 for r in result["rounds"])
    assert all(g["count"] == 0 for r in result["rounds"] for g in r["gates"].values())
    raw = json.dumps(result)
    assert all(c.text not in raw for c in fixed().cases.cases)
    assert "text" not in result and "responses" not in result
    assert result["case_version"]["combined"] == fixed().version
    assert len(result["case_version"]["input"]) == len(result["case_version"]["gold"]) == 64


@pytest.mark.parametrize(
    "count,policy,passed",
    [(5, "separate", True), (6, "separate", False), (6, "accept", True), (6, "reject", False)],
)
def test_abstain_strict_boundary(count: int, policy: str, passed: bool) -> None:
    from digital_souls_core.privacy_classifier import Assessment

    rows = observations()
    abstain = AssessmentResult(
        assessment=Assessment(
            classification="ABSTAIN",
            subject="UNKNOWN",
            category="UNKNOWN",
            policy_version="core-privacy-v1",
        )
    )
    for i in range(count):
        rows[i] = replace(rows[i], result=abstain)
    result = report(rows, policy)
    assert result["passed"] is passed
    assert result["rounds"][0]["abstain"]["count"] == count
    assert result["rounds"][1]["abstain"]["count"] == 0
    assert all(g["count"] == 0 for r in result["rounds"] for g in r["gates"].values())


@pytest.mark.parametrize("change", ["missing", "duplicate", "unknown", "unknown_run", "empty"])
def test_incomplete_results_never_pass(change: str) -> None:
    rows = observations()
    if change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows.append(rows[0])
    elif change == "unknown":
        rows.append(replace(rows[0], case_id="unknown"))
    elif change == "unknown_run":
        rows.append(replace(rows[0], run=4))
    else:
        rows = []
    result = report(rows)
    assert not result["passed"]
    assert result["gates"]["execution_incomplete"]["count"] > 0


@pytest.mark.parametrize(
    "failure,gate",
    [
        ("invalid_response", "invalid_output"),
        ("invalid_json", "invalid_output"),
        ("invalid_assessment", "invalid_output"),
        ("timeout", "timeout"),
        ("provider_exception", "execution_incomplete"),
        ("scan_refused", "execution_incomplete"),
        ("policy_mismatch", "execution_incomplete"),
    ],
)
def test_failure_gates_are_independent(failure: str, gate: str) -> None:
    rows = observations()
    rows[0] = replace(rows[0], result=AssessmentResult(failure=failure))  # type: ignore[arg-type]
    result = report(rows)
    assert not result["passed"] and result["gates"][gate]["count"] == 1
    assert result["rounds"][0]["cases"][0]["failure"] == failure


@pytest.mark.parametrize("field,value", [("subject", "GENERAL"), ("category", "OTHER_SENSITIVE")])
def test_eight_case_group_requires_eight_and_rounds_do_not_offset(field: str, value: str) -> None:
    rows = observations()
    index = next(
        i for i, g in enumerate(fixed().expectations.expectations) if g.group == "SELF_HARM"
    )
    current = rows[index].result.assessment
    assert current is not None
    rows[index] = replace(
        rows[index], result=AssessmentResult(assessment=current.model_copy(update={field: value}))
    )
    result = report(rows)
    assert not result["passed"]
    assert result["rounds"][0]["groups"]["SELF_HARM"]["accuracy"] == 7 / 8
    assert result["rounds"][1]["groups"]["SELF_HARM"]["status"] == "PASS"
    assert all(g["count"] == 0 for g in result["gates"].values())


def test_false_negative_is_zero_tolerance_even_with_group_at_ninety_percent() -> None:
    rows = observations()
    current = rows[0].result.assessment
    assert current is not None
    rows[0] = replace(
        rows[0],
        result=AssessmentResult(
            assessment=current.model_copy(
                update={"classification": "NOT_SENSITIVE", "category": "NONE"}
            )
        ),
    )
    result = report(rows)
    assert result["rounds"][0]["groups"]["HEALTH"]["status"] == "PASS"
    assert result["gates"]["false_negative"]["count"] == 1 and not result["passed"]


@pytest.mark.parametrize("policy,runs", [("", 3), ("default", 3), ("accept", 0), ("accept", 2)])
def test_configuration_cannot_weaken_contract(policy: str, runs: int) -> None:
    with pytest.raises(ValueError):
        aggregate(fixed(), observations(), runs=runs, abstain_policy=policy)


@pytest.mark.parametrize(
    "classification,subject,category,expected",
    [
        ("NOT_SENSITIVE", "GENERAL", "NONE", True),
        ("SENSITIVE", "SELF", "HEALTH", False),
        ("ABSTAIN", "UNKNOWN", "UNKNOWN", False),
    ],
)
async def test_safe_delegates_without_payload_changes(
    classification: str, subject: str, category: str, expected: bool
) -> None:
    provider = FakeProvider()
    provider.response["choices"][0]["message"]["content"] = assessment(
        classification=classification, subject=subject, category=category
    )
    classifier = LocalClassifier(provider, local_profile(), model_digest="synthetic")
    result = await classifier.assess("Synthetic input", POLICY_VERSION)
    assert result.assessment is not None and result.failure is None
    assert await classifier.safe("Synthetic input", POLICY_VERSION) is expected
    assert provider.calls[0] == provider.calls[1]
    assert provider.calls[0][1]["max_completion_tokens"] == 256
    assert provider.calls[0][1]["messages"][1] == {
        "role": "user",
        "content": json.dumps("Synthetic input"),
    }


@pytest.mark.parametrize(
    "mode,failure",
    [
        ("secret", "scan_refused"),
        ("scan", "scan_refused"),
        ("policy", "policy_mismatch"),
        ("provider", "provider_exception"),
        ("timeout", "timeout"),
        ("choices", "invalid_response"),
        ("finish", "invalid_response"),
        ("role", "invalid_response"),
        ("tools", "invalid_response"),
        ("length", "invalid_response"),
        ("content", "invalid_response"),
        ("json", "invalid_json"),
        ("duplicate", "invalid_json"),
        ("assessment", "invalid_assessment"),
    ],
)
async def test_assess_failure_kind_and_safe_invariance(mode: str, failure: str) -> None:
    provider = FakeProvider()
    provider.response["choices"][0]["message"]["content"] = assessment()
    value: object = "Synthetic input"
    policy = POLICY_VERSION
    if mode == "secret":
        value = "password: SYNTHETIC_ONLY"
    elif mode == "scan":
        value = object()
    elif mode == "policy":
        policy = "old"
    elif mode in {"provider", "timeout"}:
        provider.error = (
            TimeoutError("private-marker") if mode == "timeout" else RuntimeError("private-marker")
        )
    elif mode == "choices":
        provider.response["choices"] = []
    elif mode == "finish":
        provider.response["choices"][0]["finish_reason"] = "length"
    else:
        message = provider.response["choices"][0]["message"]
        if mode == "role":
            message["role"] = "user"
        elif mode == "tools":
            message["tool_calls"] = [True]
        else:
            message["content"] = {
                "length": "x" * 4097,
                "content": None,
                "json": "private-marker",
                "duplicate": assessment()[:-1] + ',"category":"NONE"}',
                "assessment": "{}",
            }[mode]
    classifier = LocalClassifier(provider, local_profile(), model_digest="synthetic")
    result = await classifier.assess(value, policy)
    assert result.failure == failure and result.assessment is None
    assert not await classifier.safe(value, policy)
    assert "private-marker" not in repr(result) and "SYNTHETIC_ONLY" not in repr(result)
    if mode in {"secret", "scan", "policy"}:
        assert not provider.calls


@pytest.mark.parametrize(
    "group,errors,passed",
    [
        ("HEALTH", 1, True),
        ("HEALTH", 2, False),
        ("MENTAL_STATE", 1, True),
        ("MENTAL_STATE", 2, False),
    ],
)
def test_group_accuracy_exact_integer_boundary(group: str, errors: int, passed: bool) -> None:
    rows = observations()
    indices = [
        i
        for i, g in enumerate(fixed().expectations.expectations)
        if g.group == group and g.sensitive
    ]
    for i in indices[:errors]:
        current = rows[i].result.assessment
        assert current is not None
        rows[i] = replace(
            rows[i],
            result=AssessmentResult(assessment=current.model_copy(update={"subject": "GENERAL"})),
        )
    result = report(rows)
    assert result["passed"] is passed
    assert all(g["count"] == 0 for g in result["gates"].values())


def test_reference_metrics_are_reported_and_do_not_add_gates() -> None:
    rows = observations()
    indices = [i for i, g in enumerate(fixed().expectations.expectations) if g.group == "NONE"]
    current = rows[indices[0]].result.assessment
    assert current is not None
    rows[indices[0]] = replace(
        rows[indices[0]],
        result=AssessmentResult(
            assessment=current.model_copy(
                update={"classification": "SENSITIVE", "category": "HEALTH"}
            )
        ),
    )
    for i in range(60):
        rows[i] = replace(rows[i], latency_ms=i + 1)
    result = report(rows)
    assert result["passed"]  # 9/10; FP is reference only.
    ref = result["rounds"][0]["reference"]
    assert ref["used_for_verdict"] is False and ref["false_positive"] == 1
    assert ref["subject"] == {"correct": 60, "total": 60, "accuracy": 1}
    assert ref["category"]["correct"] == 59 and ref["category"]["total"] == 60
    assert ref["latency_ms"] == {"median": 30.5, "p95": 57}
    assert set(ref["language"]) == {"ja", "en"}
    assert sum(m["correct"] for m in ref["language"].values()) == 59
    assert ref["tag"] and all(m["total"] > 0 for m in ref["tag"].values())


async def test_real_timeout_cleanup_and_cancel_propagation() -> None:
    import asyncio
    from typing import Any

    from digital_souls_core.character import Profile

    closed = asyncio.Event()
    entered = asyncio.Event()

    class Blocking(FakeProvider):
        async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
            raise AssertionError("unreachable")

    classifier = LocalClassifier(
        Blocking(),
        local_profile().model_copy(update={"timeout_seconds": 0.01}),
        model_digest="synthetic",
    )
    assert (await classifier.assess("Synthetic input", POLICY_VERSION)).failure == "timeout"
    assert closed.is_set()
    entered.clear()
    closed.clear()
    task = asyncio.create_task(classifier.safe("Synthetic input", POLICY_VERSION))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()

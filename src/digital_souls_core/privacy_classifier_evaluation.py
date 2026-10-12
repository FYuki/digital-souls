"""Pure, content-free privacy scoring; no model or judge participates in verdicts."""

import hashlib
import json
import math
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .privacy_classifier import AssessmentResult
from .privacy_evaluation_cases import EvaluationCases, Expectation

GATES = ("false_negative", "invalid_output", "timeout", "execution_incomplete")
GROUPS = ("HEALTH", "MENTAL_STATE", "SELF_HARM", "ABUSE", "FINANCIAL", "NONE")
type AbstainPolicy = Literal["accept", "reject", "separate"]
# ADR 0032: user decision C (2026-10-12). accept/reject remain for comparison only.
DEFAULT_ABSTAIN_POLICY: AbstainPolicy = "separate"


def validate_options(runs: int, abstain_policy: str) -> None:
    if type(runs) is not int or runs < 3 or abstain_policy not in {"accept", "reject", "separate"}:
        raise ValueError("at least three runs and explicit abstain policy required")


@dataclass(frozen=True)
class Observation:
    run: int
    case_id: str
    result: AssessmentResult
    latency_ms: float

    def __post_init__(self) -> None:
        if (
            type(self.run) is not int
            or self.run < 1
            or not self.case_id
            or len(self.case_id) > 128
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                for c in self.case_id
            )
            or not math.isfinite(self.latency_ms)
            or self.latency_ms < 0
        ):
            raise ValueError("invalid privacy observation address or latency")


def score(expected: Expectation, result: AssessmentResult, abstain_policy: str) -> bool:
    if abstain_policy not in {"accept", "reject", "separate"}:
        raise ValueError("explicit abstain policy required")
    assessment = result.assessment
    if assessment is None or assessment.classification not in expected.allowed_classifications:
        return False
    if assessment.classification == "ABSTAIN":
        return abstain_policy != "reject"
    return (
        assessment.subject == expected.expected_subject
        and assessment.category in expected.allowed_categories
    )


def _accuracy(correct: int, total: int) -> dict[str, Any]:
    return {
        "correct": correct,
        "total": total,
        "accuracy": correct / total if total else None,
        "status": "PASS" if total and correct * 10 >= total * 9 else "FAIL",
    }


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _violation(gates: dict[str, dict[str, Any]], gate: str, case_id: str) -> None:
    gates[gate]["count"] += 1
    gates[gate]["case_ids"].append(case_id)


def aggregate(
    cases: EvaluationCases,
    observations: Sequence[Observation],
    *,
    runs: int,
    abstain_policy: str,
) -> dict[str, Any]:
    """Every expected address must occur exactly once; other rounds cannot offset FAIL."""
    validate_options(runs, abstain_policy)
    cases = EvaluationCases.model_validate(cases.model_dump())
    gold = {g.id: g for g in cases.expectations.expectations}
    rounds: list[dict[str, Any]] = []
    fail_reasons: list[str] = []
    total_gates: dict[str, dict[str, Any]] = {g: {"count": 0, "case_ids": []} for g in GATES}
    extra_runs = [o for o in observations if o.run > runs]
    if extra_runs:
        fail_reasons.append("execution_incomplete: unknown run")
        total_gates["execution_incomplete"]["count"] += len(extra_runs)
        total_gates["execution_incomplete"]["case_ids"].extend(o.case_id for o in extra_runs)
    for run in range(1, runs + 1):
        rows = [o for o in observations if o.run == run]
        addresses = Counter(o.case_id for o in rows)
        by_id = {o.case_id: o for o in rows if addresses[o.case_id] == 1 and o.case_id in gold}
        gates: dict[str, dict[str, Any]] = {g: {"count": 0, "case_ids": []} for g in GATES}
        reasons: list[str] = []

        for case_id in sorted(set(addresses) | set(gold)):
            if case_id not in gold or addresses[case_id] != 1:
                _violation(gates, "execution_incomplete", case_id)
        scored: list[dict[str, Any]] = []
        abstain = 0
        fp = 0
        subject_correct = category_correct = definitive = 0
        for case in cases.cases.cases:
            observed = by_id.get(case.id)
            expected = gold[case.id]
            assessment = observed.result.assessment if observed else None
            failure = observed.result.failure if observed else "missing_or_duplicate"
            correct = bool(observed and score(expected, observed.result, abstain_policy))
            if failure in {"invalid_response", "invalid_json", "invalid_assessment"}:
                _violation(gates, "invalid_output", case.id)
            elif failure == "timeout":
                _violation(gates, "timeout", case.id)
            elif failure in {"scan_refused", "policy_mismatch", "provider_exception"}:
                _violation(gates, "execution_incomplete", case.id)
            if assessment:
                abstain += assessment.classification == "ABSTAIN"
                if assessment.classification != "ABSTAIN":
                    definitive += 1
                    subject_correct += assessment.subject == expected.expected_subject
                    category_correct += assessment.category in expected.allowed_categories
                if expected.sensitive and assessment.classification == "NOT_SENSITIVE":
                    _violation(gates, "false_negative", case.id)
                fp += not expected.sensitive and assessment.classification == "SENSITIVE"
            scored.append(
                {
                    "case_id": case.id,
                    "classification": assessment.classification if assessment else None,
                    "subject": assessment.subject if assessment else None,
                    "category": assessment.category if assessment else None,
                    "correct": correct,
                    "failure": failure,
                    "latency_ms": observed.latency_ms if observed else None,
                }
            )
        correct_by_id = {s["case_id"]: s["correct"] for s in scored}
        groups = {}
        for group in GROUPS:
            ids = [g.id for g in gold.values() if g.group == group]
            groups[group] = _accuracy(sum(correct_by_id[i] for i in ids), len(ids))
            if groups[group]["status"] == "FAIL":
                reasons.append(f"group_accuracy: {group}")
        for gate, findings in gates.items():
            if findings["count"]:
                reasons.append(gate)
            total_gates[gate]["count"] += findings["count"]
            total_gates[gate]["case_ids"].extend(findings["case_ids"])
        count = len(cases.cases.cases)
        if abstain_policy == "separate" and abstain * 10 >= count:
            reasons.append("abstain_rate: requires < 0.10")
        latencies = sorted(o.latency_ms for o in by_id.values())
        reference: dict[str, Any] = {"used_for_verdict": False}
        for dimension in ("language", "tag"):
            label_set: set[str]
            if dimension == "language":
                label_set = {c.language for c in cases.cases.cases}
            else:
                label_set = {tag for c in cases.cases.cases for tag in c.tags}
            labels = sorted(label_set)
            reference[dimension] = {}
            for label in labels:
                ids = [
                    c.id
                    for c in cases.cases.cases
                    if (c.language == label if dimension == "language" else label in c.tags)
                ]
                metric = _accuracy(sum(correct_by_id[i] for i in ids), len(ids))
                del metric["status"]
                reference[dimension][label] = metric
        reference.update(
            {
                "subject": {
                    "correct": subject_correct,
                    "total": definitive,
                    "accuracy": subject_correct / definitive if definitive else None,
                },
                "category": {
                    "correct": category_correct,
                    "total": definitive,
                    "accuracy": category_correct / definitive if definitive else None,
                },
                "false_positive": fp,
                "latency_ms": {
                    "median": statistics.median(latencies) if latencies else None,
                    "p95": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
                },
            }
        )
        rounds.append(
            {
                "run": run,
                "status": "FAIL" if reasons else "PASS",
                "fail_reasons": reasons,
                "overall": _accuracy(sum(s["correct"] for s in scored), count),
                "groups": groups,
                "abstain": {"count": abstain, "total": count, "rate": abstain / count},
                "gates": gates,
                "cases": scored,
                "reference": reference,
            }
        )
        fail_reasons.extend(f"run {run}: {r}" for r in reasons)
    return {
        "schema_version": 1,
        "status": "FAIL" if fail_reasons else "PASS",
        "passed": not fail_reasons,
        "fail_reasons": fail_reasons,
        "runs": runs,
        "abstain_policy": abstain_policy,
        "case_version": {
            "input": _hash(cases.cases.model_dump()),
            "gold": _hash(cases.expectations.model_dump()),
            "combined": cases.version,
        },
        "gates": total_gates,
        "rounds": rounds,
    }

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from statistics import quantiles
import sys
from time import perf_counter_ns
from types import SimpleNamespace
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.screen_perception.detector import (
    ScreenReferenceHistoryItem,
    decide_screen_reference,
)


DEFAULT_CASES = (
    REPOSITORY_ROOT
    / "contracts/perception/screen/fixtures/contextual-reference-cases.json"
)


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    expected_decision: str
    actual_decision: str
    expected_path: str
    actual_path: str
    passed: bool
    rule_latency_ms: float | None


class FixtureJudge:
    """LLM境界の呼出条件だけを検証する本文非保持の決定論的judge。"""

    def __init__(self, decision: str) -> None:
        self.decision = decision
        self.calls = 0

    def estimate_input_tokens(self, **_request: object) -> object:
        return SimpleNamespace(count=120)

    def generate_structured(self, **_request: object) -> object:
        self.calls += 1
        basis = (
            "competing_references"
            if self.decision == "clarify_reference"
            else "current_message"
        )
        return SimpleNamespace(value={"decision": self.decision, "basis": basis})


def _p95(values: list[float]) -> float:
    if len(values) == 1:
        return values[0]
    return quantiles(values, n=100, method="inclusive")[94]


def evaluate(cases_path: Path, *, rule_iterations: int = 100) -> dict[str, Any]:
    fixture = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = fixture["cases"]
    results: list[CaseResult] = []
    rule_latencies: list[float] = []
    judge_calls = 0

    for case in cases:
        history = tuple(
            ScreenReferenceHistoryItem(
                item["role"], item["content"], item["screen_provenance"]
            )
            for item in case["history"]
        )
        judge = FixtureJudge(case["expected_decision"])
        iterations = rule_iterations if case["expected_path"] == "rule" else 1
        decision = None
        durations: list[float] = []
        for _ in range(iterations):
            started = perf_counter_ns()
            decision = decide_screen_reference(
                case["current_message"],
                sharing_active=case["sharing"] == "active",
                screen_use_authorized=case["screen_use_authorized"],
                explicit_ui=case["explicit_ui"],
                history=history,
                router=judge,
                cloud_judge_history_allowed=(
                    case.get("reference_judge_destination") != "cloud"
                    or all(
                        item.screen_provenance != "expired_session"
                        for item in history
                    )
                ),
            )
            durations.append((perf_counter_ns() - started) / 1_000_000)
        if decision is None:
            raise RuntimeError("screen reference fixture must execute")
        latency = _p95(durations) if case["expected_path"] == "rule" else None
        if latency is not None:
            rule_latencies.extend(durations)
        judge_calls += judge.calls
        results.append(
            CaseResult(
                case_id=case["id"],
                expected_decision=case["expected_decision"],
                actual_decision=decision.decision,
                expected_path=case["expected_path"],
                actual_path=decision.path,
                passed=(
                    decision.decision == case["expected_decision"]
                    and decision.path == case["expected_path"]
                ),
                rule_latency_ms=latency,
            )
        )

    screen_required = [item for item in results if item.expected_decision == "inspect_screen"]
    screen_unneeded = [item for item in results if item.expected_decision != "inspect_screen"]
    clarification_unneeded = [item for item in results if item.expected_decision != "clarify_reference"]
    prohibited_or_unauthorized = [
        (case, result)
        for case, result in zip(cases, results, strict=True)
        if not case["screen_use_authorized"]
        or "prohibition" in case["id"]
    ]
    metrics = {
        "case_count": len(results),
        "case_pass_rate": sum(item.passed for item in results) / len(results),
        "false_reference_rate": (
            sum(item.actual_decision == "inspect_screen" for item in screen_unneeded)
            / len(screen_unneeded)
        ),
        "missed_reference_rate": (
            sum(item.actual_decision != "inspect_screen" for item in screen_required)
            / len(screen_required)
        ),
        "unnecessary_clarification_rate": (
            sum(item.actual_decision == "clarify_reference" for item in clarification_unneeded)
            / len(clarification_unneeded)
        ),
        "judge_call_rate": judge_calls / len(results),
        "prohibited_or_unauthorized_inspections": sum(
            result.actual_decision == "inspect_screen"
            for _case, result in prohibited_or_unauthorized
        ),
        "rule_latency_p95_ms": _p95(rule_latencies),
    }
    thresholds = {
        "case_pass_rate_min": 1.0,
        "false_reference_rate_max": 0.05,
        "missed_reference_rate_max": 0.10,
        "unnecessary_clarification_rate_max": 0.10,
        "judge_call_rate_max": 0.20,
        "prohibited_or_unauthorized_inspections_max": 0,
        "rule_latency_p95_ms_max": 20.0,
    }
    passed = (
        metrics["case_pass_rate"] >= thresholds["case_pass_rate_min"]
        and metrics["false_reference_rate"] <= thresholds["false_reference_rate_max"]
        and metrics["missed_reference_rate"] <= thresholds["missed_reference_rate_max"]
        and metrics["unnecessary_clarification_rate"]
        <= thresholds["unnecessary_clarification_rate_max"]
        and metrics["judge_call_rate"] <= thresholds["judge_call_rate_max"]
        and metrics["prohibited_or_unauthorized_inspections"]
        <= thresholds["prohibited_or_unauthorized_inspections_max"]
        and metrics["rule_latency_p95_ms"] <= thresholds["rule_latency_p95_ms_max"]
    )
    return {
        "schema_version": 1,
        "evidence_kind": "synthetic_route_conformance",
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture_contract_version": fixture["contract_version"],
        "rule_iterations": rule_iterations,
        "metrics": metrics,
        "thresholds": thresholds,
        "passed": passed,
        "cases": [asdict(item) for item in results],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rule-iterations", type=int, default=100)
    arguments = parser.parse_args()
    if arguments.rule_iterations < 1:
        parser.error("--rule-iterations must be positive")
    artifact = evaluate(arguments.cases, rule_iterations=arguments.rule_iterations)
    encoded = json.dumps(artifact, ensure_ascii=False, indent=2) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    print(json.dumps({"passed": artifact["passed"], **artifact["metrics"]}))
    return 0 if artifact["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

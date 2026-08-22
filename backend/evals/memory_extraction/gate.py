from __future__ import annotations

import argparse
import json
from pathlib import Path

RATE_NAMES = (
    "enum_match_rate",
    "topic_validity_rate",
    "hallucination_free_rate",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument(
        "--metric",
        action="append",
        choices=RATE_NAMES,
        dest="metrics",
    )
    parser.add_argument(
        "--thresholds",
        type=Path,
        default=Path(__file__).with_name("thresholds.json"),
    )
    args = parser.parse_args()
    required_metrics = tuple(args.metrics) if args.metrics else RATE_NAMES
    thresholds = json.loads(args.thresholds.read_text(encoding="utf-8"))
    payload = json.loads(args.results.read_text(encoding="utf-8"))
    results = payload.get("results")
    if not isinstance(results, dict) or not isinstance(results.get("results"), list):
        raise ValueError("results payload must contain result rows")
    totals = {name: 0 for name in required_metrics}
    passed = {name: 0 for name in required_metrics}
    for row in results["results"]:
        if not isinstance(row, dict):
            raise ValueError("result row must be an object")
        grading = row.get("gradingResult")
        if not isinstance(grading, dict):
            raise ValueError("result row must contain gradingResult")
        components = grading.get("componentResults")
        if not isinstance(components, list):
            raise ValueError("gradingResult must contain componentResults")
        seen_metrics: set[str] = set()
        for component in components:
            if not isinstance(component, dict):
                raise ValueError("component result must be an object")
            assertion = component.get("assertion")
            if not isinstance(assertion, dict):
                raise ValueError("component result must contain an assertion")
            metric = assertion.get("metric")
            if metric not in totals:
                continue
            if not isinstance(component.get("pass"), bool):
                raise ValueError(f"component result for {metric} must contain pass")
            seen_metrics.add(metric)
            totals[metric] += 1
            if component.get("pass") is True:
                passed[metric] += 1
        missing_metrics = set(required_metrics) - seen_metrics
        if missing_metrics:
            raise ValueError(
                "result row is missing required metrics: "
                + ", ".join(sorted(missing_metrics))
            )
    if any(total == 0 for total in totals.values()):
        raise ValueError("results do not contain every required metric")
    metrics = {name: passed[name] / totals[name] for name in required_metrics}
    failed = [
        name
        for name in required_metrics
        if not isinstance(metrics.get(name), (int, float))
        or metrics[name] < thresholds[name]
    ]
    if failed:
        print("memory extraction conformance failed: " + ", ".join(failed))
        return 1
    print("memory extraction conformance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

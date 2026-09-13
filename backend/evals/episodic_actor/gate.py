"""欠落・重複を許さず、合成100件の90%以上を合格とする。"""

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path

from evals.episodic_actor.score import actor_match


def summarize(payload, cases, thresholds):
    expected = {case["id"]: case["vars"] for case in cases}
    if len(expected) != len(cases) or len(cases) != thresholds["case_count"]:
        raise ValueError("dataset must contain the declared unique case count")
    rows = payload["results"]["results"]
    seen, correct, failures, latencies = set(), 0, [], []
    categories = defaultdict(lambda: {"correct": 0, "total": 0})
    identities = set()
    for row in rows:
        case_id = row.get("vars", {}).get("case_id")
        if case_id not in expected or case_id in seen:
            raise ValueError("unknown or duplicate evaluation case")
        seen.add(case_id)
        variables = expected[case_id]
        raw = row.get("response", {}).get("output")
        try:
            output = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            output = {}
        passed = actor_match(output, json.loads(variables["expected_json"]))
        # 他caseの結果やcachedな推論を性能試験の成功へ混ぜない。
        if output.get("case_id") != case_id or row.get("response", {}).get("cached"):
            passed = False
        category = categories[variables["category"]]
        category["total"] += 1
        category["correct"] += int(passed)
        correct += int(passed)
        if not passed:
            failures.append(case_id)
        seconds = output.get("latency_seconds")
        if isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds >= 0:
            latencies.append(seconds)
        identity = tuple(output.get(key) for key in ("model_id", "model_digest", "prompt_version"))
        if all(isinstance(value, str) and value for value in identity):
            identities.add(identity)
        elif passed:
            raise ValueError("successful result is missing model identity")
    if seen != set(expected):
        raise ValueError("evaluation is incomplete; missing cases cannot be omitted")
    if len(identities) > 1:
        raise ValueError("evaluation mixed model or prompt identities")
    rate = correct / len(cases)
    latencies.sort()
    return {
        "passed": rate >= thresholds["actor_exact_match_rate"],
        "correct": correct, "total": len(cases), "actor_exact_match_rate": rate,
        "threshold": thresholds["actor_exact_match_rate"], "categories": dict(categories),
        "failed_cases": failures, "identities": sorted(identities),
        "latency_seconds": {
            "p50": latencies[math.ceil(len(latencies) * .5) - 1] if latencies else None,
            "p95": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    root = Path(__file__).parent
    cases = [json.loads(line) for line in (root / "cases.jsonl").read_text().splitlines() if line]
    thresholds = json.loads((root / "thresholds.json").read_text())
    summary = summarize(json.loads(args.results.read_text()), cases, thresholds)
    if args.summary:
        args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

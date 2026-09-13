"""項目ごとに90%以上を要求し、低い分類を全体平均に埋めない。"""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path

from evals.episodic_quality.score import evaluate


def summarize(payload, cases, thresholds):
    expected = {c["id"]: c["vars"] for c in cases}
    counts = dict(Counter(v["category"] for v in expected.values()))
    if len(expected) != len(cases) or len(cases) != thresholds["case_count"] or counts != thresholds["categories"]:
        raise ValueError("case corpus does not match declared counts")
    seen = set()
    groups = defaultdict(lambda: {"correct": 0, "total": 0, "failed_cases": []})
    identities = defaultdict(set)
    times = defaultdict(list)
    privacy = {"sensitive": 0, "benign": 0, "false_negative": 0, "false_positive": 0, "abstain": 0}
    for row in payload["results"]["results"]:
        cid = row.get("vars", {}).get("case_id")
        if cid not in expected or cid in seen:
            raise ValueError("duplicate or unknown case")
        seen.add(cid)
        variables = expected[cid]
        category = variables["category"]
        try:
            output = json.loads(row.get("response", {}).get("output", "{}"))
            if not isinstance(output, dict):
                output = {}
            passed = evaluate(output, variables)
        except (ValueError, TypeError, KeyError, StopIteration):
            output, passed = {}, False
        if output.get("case_id") != cid or row.get("response", {}).get("cached"):
            passed = False
        identity = tuple(output.get(k) for k in ("model_id", "model_digest", "prompt_version", "policy_version"))
        stage = "privacy" if category == "privacy" else "memory"
        if all(isinstance(x, str) and x for x in identity[:3]):
            identities[stage].add(identity)
        elif passed:
            raise ValueError("successful output lacks inference provenance")
        item = groups[category]
        item["correct"] += int(passed)
        item["total"] += 1
        if not passed:
            item["failed_cases"].append(cid)
        elapsed = output.get("latency_seconds")
        if isinstance(elapsed, (int, float)) and math.isfinite(elapsed) and elapsed >= 0:
            times[category].append(elapsed)
        if category == "privacy":
            sensitive = json.loads(variables["expected_json"])["classification"] == "SENSITIVE"
            result = output.get("classification")
            privacy["sensitive" if sensitive else "benign"] += 1
            privacy["false_negative"] += int(sensitive and result == "NOT_SENSITIVE")
            privacy["false_positive"] += int(not sensitive and result == "SENSITIVE")
            privacy["abstain"] += int(result not in {"SENSITIVE", "NOT_SENSITIVE"})
    if seen != set(expected):
        raise ValueError("incomplete evaluation")
    if any(len(values) > 1 for values in identities.values()):
        raise ValueError("mixed model, prompt or policy identities")
    for name, item in groups.items():
        item["accuracy"] = item["correct"] / item["total"]
        item["passed"] = item["accuracy"] >= thresholds["accuracy"]
        elapsed = sorted(times[name])
        item["latency_seconds"] = {
            "p50": elapsed[math.ceil(len(elapsed) * .5) - 1] if elapsed else None,
            "p95": elapsed[math.ceil(len(elapsed) * .95) - 1] if elapsed else None,
        }
    # 既存privacyの誤許可・誤拒否・棄権基準も維持する。90%だけで誤許可を相殺しない。
    privacy["evaluated"] = bool(counts.get("privacy"))
    privacy["existing_limits_passed"] = not privacy["evaluated"] or (
        privacy["abstain"] / counts["privacy"] < .1
        and privacy["false_negative"] / privacy["sensitive"] < .05
        and privacy["false_positive"] / privacy["benign"] < .2
    )
    return {"passed": all(g["passed"] for g in groups.values()) and privacy["existing_limits_passed"],
            "threshold": thresholds["accuracy"], "total": len(cases),
            "correct": sum(g["correct"] for g in groups.values()), "categories": dict(groups),
            "privacy": privacy, "identities": {k: list(v) for k, v in identities.items()}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--categories", nargs="+")
    args = parser.parse_args()
    root = Path(__file__).parent
    cases = [json.loads(l) for l in (root / "cases.jsonl").read_text().splitlines() if l]
    thresholds = json.loads((root / "thresholds.json").read_text())
    if args.categories:
        selected = set(args.categories)
        if not selected <= set(thresholds["categories"]):
            raise ValueError("unknown category")
        cases = [c for c in cases if c["vars"]["category"] in selected]
        thresholds = thresholds | {
            "case_count": len(cases),
            "categories": {k: v for k, v in thresholds["categories"].items() if k in selected},
        }
    report = summarize(json.loads(args.results.read_text()), cases, thresholds)
    if args.summary:
        args.summary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

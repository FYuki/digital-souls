"""第2段階の固定記憶参照を閾値なしで集計する。第1段階の合否を変更しない。"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import re
from itertools import combinations
from pathlib import Path

from app.voice_baseline import _assert_anonymous, _load_trace
from app.voice_metrics import aggregate_metric
from app.voice_quality_reference_state import FIXTURE, FIXTURE_VERSION, MEMORY_IDS, digest
from app.memory.persistence.schema import PERSONA_MEMORY_TABLES
from report_normal_diagnostic import definitions, observations


def validate_reference_state(trial):
    state = trial.get("initial_state_evidence")
    if not isinstance(state, dict) or state.get("method") != "sqlite_fixed_reference_state_v1":
        raise ValueError("fixed reference evidence unavailable")
    if digest(state) != trial.get("initial_state_hash"):
        raise ValueError("fixed reference state hash mismatch")
    if (state.get("rag_enabled") is not True or state.get("fixture_version") != FIXTURE_VERSION
            or state.get("fixture_sha256") != digest(FIXTURE)
            or state.get("memory_count") != 3 or state.get("index_count") != 3
            or state.get("memory_scheduler_policy") != {"method": "controlled_memory_schedulers_v1",
                "formation_disabled": True, "consolidation_disabled": True}):
        raise ValueError("fixed reference conditions differ")
    counts = state.get("row_counts", {})
    nonempty = {"approved_memories", "memory_sources", "memory_write_receipts", "memory_index_outbox"}
    if (not isinstance(counts, dict) or set(counts) != set(PERSONA_MEMORY_TABLES)
            or any(type(counts[key]) is not int or counts[key] != (3 if key in nonempty else 0) for key in counts)):
        raise ValueError("fixed reference row counts differ")
    for key in ("approved_rows_sha256", "source_rows_sha256"):
        if not isinstance(state.get(key), str) or not re.fullmatch("[a-f0-9]{64}", state[key]):
            raise ValueError("fixed reference content hash unavailable")
    root = Path(__file__).resolve().parents[2]
    configuration = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                     for name in ("characters/miori/miori.card.json", "backend/app/memory/memory_policy.json")}
    if state.get("configuration_sha256") != configuration:
        raise ValueError("fixed reference configuration differs")
    reference = trial.get("memory_reference_observation")
    if (not isinstance(reference, dict) or reference.get("method") != "response_prompt_memory_dependencies_v1"
            or reference.get("fixture_version") != FIXTURE_VERSION or reference.get("response_count") != 1
            or type(reference.get("referenced_count")) is not int
            or not 0 <= reference["referenced_count"] <= 3
            or reference.get("reference_outcome") != ("referenced" if reference["referenced_count"] else "not_referenced")):
        raise ValueError("response reference evidence unavailable")
    expected_versions = {digest([(str(value), 1) for value in subset])
                         for subset in combinations(MEMORY_IDS, reference["referenced_count"])}
    if reference.get("reference_versions_sha256") not in expected_versions:
        raise ValueError("response reference versions differ")


def summarize(manifest, trace, baseline):
    measured_count = manifest.get("expected_measured")
    trials = manifest.get("trials", [])
    if (manifest.get("measurement_scope") != "fixed_memory_reference_measurement"
            or manifest.get("expected_warmup") != 5 or type(measured_count) is not int
            or not 1 <= measured_count <= 100 or len(trials) != 5 + measured_count
            or [t.get("phase") for t in trials] != ["warmup"] * 5 + ["measured"] * measured_count):
        raise ValueError("planned independent reference trials required")
    successful = [trial for trial in trials if trial.get("outcome") == "success"]
    for key in ("sessionId", "utteranceId", "responseId", "conversationId"):
        if len({trial.get(key) for trial in successful}) != len(successful):
            raise ValueError("reference trial identities reused")
    if any(trial.get("outcome") not in ("success", "failure") for trial in trials):
        raise ValueError("trial outcome missing")
    measured = trials[5:]
    rows = [observations(trial, trace, manifest["fixture"], manifest["initial_state_hash"],
                         initial_state_validator=validate_reference_state) for trial in measured]
    metrics = [aggregate_metric(definition.name, [row[definition.name] for row, _ in rows],
        unit=definition.unit, start_point=definition.start_point, end_point=definition.end_point
    ).model_dump(mode="json") for definition in definitions()]
    if (baseline.get("profile") != "integration-irodori-cuda-graph"
            or baseline.get("measurement_kind") != "controlled_baseline"
            or baseline.get("fixture_version") != manifest["fixture"]["fixture_version"]
            or baseline.get("quantile_method") != "hyndman_fan_type_7"
            or baseline.get("run_counts") != {"warmup": 5, "measured": 100, "success": 100, "failure": 0, "excluded": 0}):
        raise ValueError("empty-state baseline does not match")
    prior = next(metric for metric in baseline["metrics"] if metric["name"] == "ttfa")
    current = next(metric for metric in metrics if metric["name"] == "ttfa")
    if any(prior.get(key) != current.get(key) for key in ("unit", "start_point", "end_point")):
        raise ValueError("baseline measurement boundary differs")
    if prior.get("success_count") != 100 or prior.get("missing_count") != 0:
        raise ValueError("baseline coverage unavailable")
    result = {"scope": "fixed_memory_reference_measurement", "quantile_method": "hyndman_fan_type_7",
        "measurement_revision": manifest["measurement_revision"],
        "fixture_version": FIXTURE_VERSION, "fixture_sha256": digest(FIXTURE),
        "planned_measured": measured_count, "recorded_measured": len(measured),
        "warmup_count": 5, "warmup_success": sum(t.get("outcome") == "success" for t in trials[:5]),
        "success": sum(t.get("outcome") == "success" for t in measured),
        "failure": sum(t.get("outcome") == "failure" for t in measured),
        "validated_count": sum(reason is None for _, reason in rows),
        "evidence_reasons": dict(Counter(reason for _, reason in rows if reason)),
        "reference_outcomes": dict(Counter(
            trial["memory_reference_observation"]["reference_outcome"] if reason is None else "missing"
            for trial, (_, reason) in zip(measured, rows))),
        "metrics": metrics, "acceptance_applicable": False, "acceptance_threshold_ms": None,
        "empty_state_comparison": {"baseline_run_id": baseline["run_id"], "baseline_count": 100,
            "p50_ms": prior["p50"], "p95_ms": prior["p95"],
            "delta_p50_ms": None if current["p50"] is None else current["p50"] - prior["p50"],
            "delta_p95_ms": None if current["p95"] is None else current["p95"] - prior["p95"]},
        "interpretation": "失敗・未参照・欠測を全予定数へ残す。時間は証拠検証済みの成功例の分布。閾値なしの追加計測であり、形成負荷・長履歴・人の聴感は評価しない。"}
    _assert_anonymous(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort-root", type=Path, required=True)
    parser.add_argument("--baseline-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.cohort_root / "trial-manifest.json").read_text())
    baseline_bytes = args.baseline_report.read_bytes()
    baseline = json.loads(baseline_bytes)
    result = summarize(manifest, _load_trace(args.cohort_root / "controlled-trace.jsonl"), baseline)
    result["baseline_report_sha256"] = hashlib.sha256(baseline_bytes).hexdigest()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import sys
import hashlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts/voice_quality"))
import report_memory_reference as reporter
from app.voice_quality_reference_state import FIXTURE, FIXTURE_VERSION, MEMORY_IDS, digest
from app.memory.persistence.schema import PERSONA_MEMORY_TABLES


def baseline():
    definition = next(d for d in reporter.definitions() if d.name == "ttfa")
    return {"profile": "integration-irodori-cuda-graph", "measurement_kind": "controlled_baseline",
        "fixture_version": "speech-v2", "quantile_method": "hyndman_fan_type_7",
        "run_id": "baseline", "run_counts": {"warmup": 5, "measured": 100, "success": 100, "failure": 0, "excluded": 0},
        "metrics": [{"name": "ttfa", "unit": definition.unit, "start_point": definition.start_point,
                     "end_point": definition.end_point, "success_count": 100, "missing_count": 0,
                     "p50": 1796.15, "p95": 1967.65}]}


def manifest():
    return {"measurement_scope": "fixed_memory_reference_measurement", "measurement_revision": "a" * 40,
        "expected_warmup": 5, "expected_measured": 20, "fixture": {"fixture_version": "speech-v2"},
        "initial_state_hash": "fixed",
        "trials": [{"phase": "warmup" if i < 5 else "measured", "outcome": "failure"} for i in range(25)]}


def test_failed_trials_keep_denominator_and_never_get_threshold():
    result = reporter.summarize(manifest(), [], baseline())
    assert result["planned_measured"] == result["recorded_measured"] == result["failure"] == 20
    assert result["validated_count"] == 0
    assert result["reference_outcomes"] == {"missing": 20}
    assert result["acceptance_applicable"] is False
    assert result["acceptance_threshold_ms"] is None
    assert result["empty_state_comparison"]["delta_p95_ms"] is None
    for metric in result["metrics"]:
        assert metric["trial_count"] == 20
        assert metric["p95"] is None


@pytest.mark.parametrize("mutation", ["remove_trial", "change_phase", "change_scope"])
def test_plan_cannot_be_reduced_after_failure(mutation):
    value = manifest()
    if mutation == "remove_trial": value["trials"].pop()
    elif mutation == "change_phase": value["trials"][0]["phase"] = "measured"
    else: value["measurement_scope"] = "controlled"
    with pytest.raises(ValueError, match="planned"):
        reporter.summarize(value, [], baseline())


@pytest.mark.parametrize("mutation", ["clock", "fixture", "coverage"])
def test_comparison_requires_same_boundary_and_verified_baseline(mutation):
    value = baseline()
    if mutation == "clock": value["metrics"][0]["start_point"] = "first_token"
    elif mutation == "fixture": value["fixture_version"] = "other"
    else: value["metrics"][0]["success_count"] = 99
    with pytest.raises(ValueError, match="baseline"):
        reporter.summarize(manifest(), [], value)


def reference_trial(count):
    evidence = {"method": "sqlite_fixed_reference_state_v1", "rag_enabled": True,
        "fixture_version": FIXTURE_VERSION, "fixture_sha256": digest(FIXTURE),
        "memory_count": 3, "index_count": 3, "memory_scheduler_policy": {
            "method": "controlled_memory_schedulers_v1", "formation_disabled": True, "consolidation_disabled": True}}
    root = Path(__file__).resolve().parents[3]
    evidence.update(row_counts={key: 3 if key in {"approved_memories", "memory_sources", "memory_write_receipts", "memory_index_outbox"} else 0 for key in PERSONA_MEMORY_TABLES},
        approved_rows_sha256="a" * 64, source_rows_sha256="b" * 64,
        configuration_sha256={name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("characters/miori/miori.card.json", "backend/app/memory/memory_policy.json")})
    versions = digest([(str(value), 1) for value in MEMORY_IDS[:count]]) if type(count) is int else ""
    return {"initial_state_evidence": evidence, "initial_state_hash": digest(evidence),
        "memory_reference_observation": {"method": "response_prompt_memory_dependencies_v1",
            "fixture_version": FIXTURE_VERSION, "response_count": 1, "referenced_count": count,
            "reference_outcome": "referenced" if count else "not_referenced", "reference_versions_sha256": versions}}


@pytest.mark.parametrize("count", [0, 1, 3])
def test_unreferenced_result_is_valid_evidence_and_not_retried(count):
    reporter.validate_reference_state(reference_trial(count))


@pytest.mark.parametrize("count", [True, -1, 4, "3"])
def test_reference_count_requires_real_bounded_number(count):
    with pytest.raises(ValueError):
        reporter.validate_reference_state(reference_trial(count))


def test_state_hash_detects_mutation():
    trial = reference_trial(3)
    trial["initial_state_evidence"]["rag_enabled"] = False
    with pytest.raises(ValueError, match="hash"):
        reporter.validate_reference_state(trial)


def test_reference_versions_cannot_be_substituted():
    trial = reference_trial(3)
    trial["memory_reference_observation"]["reference_versions_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="versions"):
        reporter.validate_reference_state(trial)

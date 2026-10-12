"""Fail-closed scoring and report contracts, without database or sockets."""

import copy
import json
from pathlib import Path
from typing import Literal

import pytest

from digital_souls_core.semantic_evaluation_cases import CaseExpectation, load_evaluation_cases
from digital_souls_core.semantic_retrieval_evaluation import (
    EvaluationReport,
    Observation,
    aggregate_run,
    independent_relevance,
    score_case,
    select_embedding,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = load_evaluation_cases(
    ROOT / "evals/semantic/cases.json", ROOT / "evals/semantic/expectations.json"
)
pytestmark = pytest.mark.ut


def gold(case_id: str) -> CaseExpectation:
    return next(g for g in DATA.expectations.cases if g.id == case_id)


def observation(case_id: str) -> Observation:
    g = gold(case_id)
    return Observation(
        retrieved_ids=g.relevant_ids,
        fact_ids={identifier: g.required_fact_ids for identifier in g.relevant_ids},
        scores={identifier: 1.0 for identifier in g.relevant_ids},
        verified_ids=g.relevant_ids,
        dispatch_valid=g.dispatch.valid,
        context_matches=True,
    )


def test_all_relevant_required_and_forbidden_facts() -> None:
    g = gold("valid-fact-attachment")
    row = score_case(g, observation(g.id))
    assert row.passed and all(row.gates.values())
    o = observation(g.id)
    assert not score_case(
        g,
        o.model_copy(
            update={"retrieved_ids": (), "fact_ids": {}, "scores": {}, "verified_ids": ()}
        ),
    ).passed
    assert not score_case(g, o.model_copy(update={"fact_ids": {"trip-episode": ()}})).passed
    forbidden = gold("stale-fact-link")
    o = observation(forbidden.id)
    row = score_case(
        forbidden, o.model_copy(update={"fact_ids": {"trip-episode": ("destination-fact",)}})
    )
    assert not row.gates["verified_records"] and not row.passed


@pytest.mark.parametrize(
    "violation", ["forbidden", "threshold", "unverified", "order", "dispatch", "context"]
)
def test_mandatory_gates_cannot_be_offset(violation: str) -> None:
    g = gold("equivalent-band-order" if violation == "order" else "private-source")
    data = observation(g.id).model_dump()
    if violation == "forbidden":
        data["retrieved_ids"] += g.forbidden_ids
        data["scores"][g.forbidden_ids[0]] = 1.0
        data["fact_ids"][g.forbidden_ids[0]] = ()
    elif violation == "threshold":
        data["scores"][g.relevant_ids[0]] = 0.51999
    elif violation == "unverified":
        data["verified_ids"] = ()
    elif violation == "order":
        data["retrieved_ids"] = tuple(reversed(g.relevant_ids))
    elif violation == "dispatch":
        data["dispatch_valid"] = False
    else:
        data["context_matches"] = False
    assert not score_case(g, Observation.model_validate(data)).passed


def test_normal_empty_search_passes_but_empty_run_is_rejected() -> None:
    g = gold("unrelated-observatory")
    assert score_case(g, observation(g.id)).passed
    with pytest.raises(ValueError):
        aggregate_run(DATA, ())


@pytest.mark.parametrize(
    "invalid",
    ["missing", "duplicate", "unknown", "error", "nan", "inf", "duplicate-result", "missing-score"],
)
def test_report_rejects_incomplete_or_invalid_results(invalid: str) -> None:
    rows = [score_case(g, observation(g.id)) for g in DATA.expectations.cases]
    if invalid == "missing":
        rows.pop()
    elif invalid == "duplicate":
        rows.append(rows[0])
    elif invalid == "unknown":
        rows[0] = rows[0].model_copy(update={"id": "unknown"})
    else:
        data = copy.deepcopy(observation(rows[0].id).model_dump())
        if invalid == "error":
            data["error"] = "evaluation_failed"
        elif invalid in {"nan", "inf"}:
            data["scores"][rows[0].retrieved_ids[0]] = float(invalid)
        elif invalid == "duplicate-result":
            data["retrieved_ids"] += data["retrieved_ids"]
        else:
            data["scores"] = {}
        with pytest.raises(ValueError):
            score_case(gold(rows[0].id), Observation.model_validate(data))
        return
    with pytest.raises(ValueError):
        aggregate_run(DATA, tuple(rows))


def test_each_category_must_reach_ninety_percent_without_averaging() -> None:
    rows = [score_case(g, observation(g.id)) for g in DATA.expectations.cases]
    for index, row in enumerate(rows):
        if row.category == "synonym" and row.id in {"synonym-02", "synonym-03"}:
            o = observation(row.id).model_copy(
                update={"retrieved_ids": (), "fact_ids": {}, "scores": {}, "verified_ids": ()}
            )
            rows[index] = score_case(gold(row.id), o)
    summary = aggregate_run(DATA, tuple(rows))
    assert not summary.passed
    assert summary.categories["synonym"].rate == 0.8
    assert summary.categories["paraphrase"].rate == 1.0


@pytest.mark.parametrize("vector", [(0.0, 0.0), (float("nan"), 1.0), (True, 1.0), (1.0,)])
def test_independent_check_rejects_invalid_vectors(vector: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        independent_relevance((1.0, 0.0), vector)


def test_independent_unit_l2_verification() -> None:
    assert independent_relevance((10.0, 0.0), (2.0, 0.0)) == 1.0
    assert independent_relevance((1.0, 0.0), (-1.0, 0.0)) == pytest.approx(1 / 3)
    assert independent_relevance((1e300, 0.0), (1e-300, 0.0)) == 1.0


def test_default_embedding_is_fixture_not_quality_evidence() -> None:
    embedding, mode = select_embedding(DATA, None)
    assert mode == "fixture" and embedding.space.dimensions == 4


@pytest.mark.parametrize("enabled", [False, None])
def test_disabled_profile_has_no_fallback(tmp_path: Path, enabled: bool | None) -> None:
    data = json.loads((ROOT / "examples/embedding.example.json").read_text())
    if enabled is None:
        data.pop("enabled", None)
    else:
        data["enabled"] = enabled
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Invalid evaluation profile"):
        select_embedding(DATA, path)


def test_explicit_enabled_profile_uses_local_adapter(tmp_path: Path) -> None:
    data = json.loads((ROOT / "examples/embedding.example.json").read_text())
    data["enabled"] = True
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data))
    embedding, mode = select_embedding(DATA, path)
    assert mode == "local_model" and embedding.space.model == data["model"]


def valid_report() -> EvaluationReport:
    from dataclasses import asdict

    from digital_souls_core.memory_ranking import RetrievalPolicy
    from digital_souls_core.semantic_retrieval_evaluation import (
        Commit,
        EvaluationReport,
        case_version,
    )

    run = aggregate_run(
        DATA, tuple(score_case(g, observation(g.id)) for g in DATA.expectations.cases)
    )
    embedding, mode = select_embedding(DATA, None)
    return EvaluationReport(
        commit=Commit(sha="a" * 40, dirty=True),
        mode=mode,
        quality_evidence=False,
        space=asdict(embedding.space),
        retrieval=asdict(RetrievalPolicy()),
        case_version=case_version(
            DATA, ROOT / "evals/semantic/cases.json", ROOT / "evals/semantic/expectations.json"
        ),
        runs=(run, run, run),
        embedding_call_count=183,
        passed=True,
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "zero-runs",
        "missing-run",
        "empty-cases",
        "duplicate-case",
        "forged-pass",
        "forged-category",
        "error",
        "nan",
        "quality-evidence",
        "wrong-policy",
        "wrong-space",
    ],
)
def test_report_revalidation_rejects_invalid_or_forged_success(invalid: str) -> None:
    from digital_souls_core.semantic_retrieval_evaluation import EvaluationReport, validate_report

    payload = valid_report().model_dump(mode="json")
    if invalid == "zero-runs":
        payload["runs"] = []
    elif invalid == "missing-run":
        payload["runs"].pop()
    elif invalid == "empty-cases":
        payload["runs"][0]["cases"] = []
    elif invalid == "duplicate-case":
        payload["runs"][0]["cases"][1] = payload["runs"][0]["cases"][0]
    elif invalid == "forged-pass":
        payload["runs"][0]["cases"][0]["gates"]["threshold"] = False
    elif invalid == "forged-category":
        payload["runs"][0]["categories"]["synonym"]["rate"] = 0.9
    elif invalid in {"error", "nan"}:
        row = payload["runs"][0]["cases"][0]
        if invalid == "error":
            row["error"] = "evaluation_failed"
        else:
            row["scores"][row["retrieved_ids"][0]] = float("nan")
    elif invalid == "quality-evidence":
        payload["quality_evidence"] = True
    elif invalid == "wrong-policy":
        payload["retrieval"]["relevance_threshold"] = 0.1
    else:
        payload["space"]["dimensions"] = 0
    with pytest.raises(ValueError):
        report = EvaluationReport.model_validate_json(json.dumps(payload))
        validate_report(report, DATA, 3)


def test_valid_report_roundtrip_is_body_safe_and_normal_zero_results_are_valid() -> None:
    from digital_souls_core.semantic_retrieval_evaluation import EvaluationReport, validate_report

    report = valid_report()
    serialized = report.model_dump_json()
    validate_report(EvaluationReport.model_validate_json(serialized), DATA, 3)
    assert "query" not in serialized and "vector" not in serialized
    assert report.classifier == "synthetic" and not report.quality_evidence
    assert report.commit.dirty


def test_required_fact_on_unrelated_candidate_does_not_count() -> None:
    g = gold("valid-fact-attachment")
    o = observation(g.id).model_copy(
        update={
            "retrieved_ids": ("trip-episode", "distractor"),
            "fact_ids": {"trip-episode": (), "distractor": ("destination-fact",)},
            "scores": {"trip-episode": 1.0, "distractor": 1.0},
            "verified_ids": ("trip-episode", "distractor"),
        }
    )
    assert not score_case(g, o).quality_passed


def test_empty_search_cannot_hide_eligible_above_threshold_candidate() -> None:
    g = gold("unrelated-observatory")
    row = score_case(g, observation(g.id).model_copy(update={"eligible_above_threshold": 1}))
    assert row.quality_passed and not row.gates["no_match"] and not row.passed


@pytest.mark.parametrize("runs", [0, -1, True])
def test_invalid_run_count_is_rejected_without_database(runs: int) -> None:
    from digital_souls_core.semantic_retrieval_evaluation import validate_report

    with pytest.raises(ValueError):
        validate_report(valid_report(), DATA, runs)


async def test_fixture_and_recording_wrapper_always_call_embedding_again() -> None:
    from digital_souls_core.semantic_evaluation_runtime import FixtureEmbedding, RecordingEmbedding

    class Counted(FixtureEmbedding):
        count = 0

        async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            self.count += 1
            return await super().embed(texts)

    delegate = Counted(DATA)
    wrapper = RecordingEmbedding(delegate)
    query = DATA.cases.cases[0].query
    assert await wrapper.embed((query,)) == await wrapper.embed((query,))
    assert delegate.count == len(wrapper.calls) == 2


@pytest.mark.parametrize("runs", [0, -1, True])
async def test_evaluator_rejects_invalid_run_count_before_storage(runs: int) -> None:
    from digital_souls_core.postgres_db import PostgresConfig
    from digital_souls_core.semantic_retrieval_evaluation import evaluate_retrieval

    report = valid_report()
    embedding, _ = select_embedding(DATA, None)
    with pytest.raises(ValueError, match="Invalid evaluation configuration"):
        await evaluate_retrieval(
            DATA,
            PostgresConfig(host="/dev/shm", database="synthetic", user="synthetic"),
            embedding,
            runs=runs,
            commit=report.commit,
            case_version=report.case_version,
        )


async def test_fixture_cannot_be_declared_as_real_model_evidence() -> None:
    from digital_souls_core.postgres_db import PostgresConfig
    from digital_souls_core.semantic_retrieval_evaluation import evaluate_retrieval

    report = valid_report()
    embedding, _ = select_embedding(DATA, None)
    with pytest.raises(ValueError, match="Invalid evaluation configuration"):
        await evaluate_retrieval(
            DATA,
            PostgresConfig(host="/dev/shm", database="synthetic", user="synthetic"),
            embedding,
            mode="local_model",
            commit=report.commit,
            case_version=report.case_version,
        )


def test_ninety_percent_boundary_is_inclusive_but_one_gate_failure_is_global() -> None:
    rows = [score_case(g, observation(g.id)) for g in DATA.expectations.cases]
    index = next(i for i, row in enumerate(rows) if row.id == "synonym-02")
    o = observation(rows[index].id).model_copy(
        update={
            "retrieved_ids": (),
            "scores": {},
            "fact_ids": {},
            "verified_ids": (),
        }
    )
    rows[index] = score_case(gold(rows[index].id), o)
    summary = aggregate_run(DATA, tuple(rows))
    assert not summary.passed and summary.categories["synonym"].rate == 0.9
    assert not summary.gates_passed  # Missing top-one is now a mandatory failure.
    index = next(i for i, row in enumerate(rows) if row.id == "synonym-03")
    o = observation(rows[index].id).model_copy(update={"scores": {"target": 0.51}})
    rows[index] = score_case(gold(rows[index].id), o)
    summary = aggregate_run(DATA, tuple(rows))
    assert summary.categories["synonym"].rate == 0.9
    assert not summary.gates_passed and not summary.passed


@pytest.mark.parametrize("mode", ["fixture", "local_model"])
def test_retrieval_order_gate_depends_on_mode(mode: Literal["fixture", "local_model"]) -> None:
    g = gold("equivalent-band-order")
    o = observation(g.id).model_copy(update={"retrieved_ids": tuple(reversed(g.relevant_ids))})
    row = score_case(g, o, mode=mode)
    assert row.gates["top_one"]
    assert row.passed == (mode == "local_model")
    assert ("expected_order" in row.gates) == (mode == "fixture")


def test_local_retrieval_accepts_extra_but_missing_top_one_fails() -> None:
    g = gold("synonym-02")
    o = observation(g.id).model_copy(
        update={
            "retrieved_ids": ("distractor", "target"),
            "fact_ids": {"distractor": (), "target": ()},
            "scores": {"distractor": 1.0, "target": 1.0},
            "verified_ids": ("distractor", "target"),
        }
    )
    assert score_case(g, o, mode="local_model").passed
    o = o.model_copy(
        update={
            "retrieved_ids": ("distractor",),
            "fact_ids": {"distractor": ()},
            "scores": {"distractor": 1.0},
        }
    )
    row = score_case(g, o, mode="local_model")
    assert not row.gates["top_one"] and not row.passed


def test_local_order_report_is_rescored_using_report_mode() -> None:
    from digital_souls_core.semantic_retrieval_evaluation import validate_report

    rows = []
    for g in DATA.expectations.cases:
        o = observation(g.id)
        if g.expected_order:
            o = o.model_copy(update={"retrieved_ids": tuple(reversed(g.expected_order))})
        rows.append(score_case(g, o, mode="local_model"))
    run = aggregate_run(DATA, tuple(rows), mode="local_model")
    report = valid_report().model_copy(
        update={"mode": "local_model", "quality_evidence": True, "runs": (run, run, run)}
    )
    validate_report(report, DATA, 3)
    with pytest.raises(ValueError):
        validate_report(
            report.model_copy(update={"mode": "fixture", "quality_evidence": False}), DATA, 3
        )


def test_top_one_alone_does_not_replace_relevant_subset_quality() -> None:
    g = gold("equivalent-band-order")
    assert g.expected_order
    identifier = g.expected_order[0]
    o = observation(g.id).model_copy(
        update={
            "retrieved_ids": (identifier,),
            "fact_ids": {identifier: ()},
            "scores": {identifier: 1.0},
            "verified_ids": (identifier,),
        }
    )
    row = score_case(g, o, mode="local_model")
    assert row.gates["top_one"]
    assert not row.quality_passed and not row.passed


@pytest.mark.parametrize("mode", ["fixture", "local_model"])
def test_no_match_gates_are_unchanged_in_both_modes(
    mode: Literal["fixture", "local_model"],
) -> None:
    g = gold("unrelated-observatory")
    o = observation(g.id)
    assert score_case(g, o, mode=mode).passed
    row = score_case(g, o.model_copy(update={"eligible_above_threshold": 1}), mode=mode)
    assert row.gates["top_one"] and not row.gates["no_match"] and not row.passed

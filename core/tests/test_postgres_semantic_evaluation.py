"""Required real PostgreSQL fixture evaluation, never model quality evidence."""

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from digital_souls_core.application import CoreError
from digital_souls_core.postgres_db import PostgresConfig
from digital_souls_core.semantic_evaluation_cases import EvaluationCase
from digital_souls_core.semantic_evaluation_runtime import FixtureEmbedding, isolated_case
from digital_souls_core.semantic_retrieval_evaluation import (
    EvaluationReport,
    observe,
    validate_report,
)

from .test_semantic_retrieval_evaluation import DATA, ROOT

pytestmark = pytest.mark.postgres


def config() -> PostgresConfig:
    return PostgresConfig(
        host=os.environ["DSC_TEST_POSTGRES_SOCKET"],
        port=int(os.environ["DSC_TEST_POSTGRES_PORT"]),
        database=os.environ["DSC_TEST_POSTGRES_DATABASE"],
        user=os.environ["DSC_TEST_POSTGRES_USER"],
    )


def case(identifier: str) -> EvaluationCase:
    return next(c for c in DATA.cases.cases if c.id == identifier)


def test_required_cli_all_sixty_two_cases_three_uncached_runs(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/evaluate-semantic-retrieval.py"),
            "--runs",
            "3",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout == process.stderr == ""
    report = EvaluationReport.model_validate_json(output.read_text())
    validate_report(report, DATA, 3)
    assert report.passed and report.classifier == "synthetic"
    assert report.mode == "fixture" and not report.quality_evidence
    assert report.embedding_call_count > 0
    assert all(len(run.cases) == 62 and run.gates_passed for run in report.runs)
    assert all(summary.rate == 1.0 for run in report.runs for summary in run.categories.values())
    serialized = output.read_text()
    for c in DATA.cases.cases:
        assert c.query not in serialized
        assert all(
            r.normalized_text not in serialized for r in (*c.episodes, *c.facts, *c.semantics)
        )
    assert "vectors" not in serialized
    # Every run must embed fresh inputs: these counts are identical, not cached.
    assert report.embedding_call_count == 3 * sum(
        c.id != "source-epoch-changed" for c in DATA.cases.cases
    )


async def test_colliding_ids_bindings_and_different_texts_are_isolated() -> None:
    first, second = case("synonym-02"), case("synonym-03")
    assert first.binding == second.binding and first.episodes[0].id == second.episodes[0].id
    with isolated_case(config(), first, FixtureEmbedding(DATA)) as a:
        with isolated_case(config(), second, FixtureEmbedding(DATA)) as b:
            a_result, _ = await a.search_context()
            b_result, _ = await b.search_context()
            assert a_result[0].identifier == b_result[0].identifier == "target"
            assert a_result[0].record.normalized_text != b_result[0].record.normalized_text
            assert a.records.retrievable(first.binding.to_domain()) != b.records.retrievable(
                second.binding.to_domain()
            )
            assert not a.records.current(first.binding.to_domain(), b_result)
            assert not b.records.current(second.binding.to_domain(), a_result)
        assert a.records.current(first.binding.to_domain(), a_result)


async def test_generated_ids_trusted_clock_and_episode_evidence_are_remapped() -> None:
    c = case("derived-semantic")
    with isolated_case(config(), c, FixtureEmbedding(DATA)) as runtime:
        assert all(logical != generated for logical, generated in runtime.conversation_ids.items())
        candidates = runtime.records.retrievable(c.binding.to_domain())
        assert len(candidates) == 3
        for candidate in candidates:
            assert all(
                cit.source.reference.conversation_id in runtime.conversation_ids.values()
                for cit in candidate.citations
            )
        semantic = next(r for r in candidates if r.identifier == "quiet-preference")
        assert len(semantic.evidence) == 2
        for conv in c.conversations:
            snapshot = runtime.history.read(
                conv.binding.to_domain(), runtime.conversation_ids[conv.id]
            )
            assert snapshot.memory_sources[0].stated_at == conv.messages[0].stated_at
        result = await observe(runtime)
        assert result.retrieved_ids == ("quiet-preference",) and result.dispatch_valid


async def test_append_exclusion_probe_and_source_deletion_use_public_ports() -> None:
    c = case("excluded-source")
    with isolated_case(config(), c, FixtureEmbedding(DATA)) as runtime:
        assert runtime.rejected_ids == ("excluded-source-hidden",)
        result, _ = await runtime.search_context()
        assert tuple(r.identifier for r in result) == ("excluded-source-visible",)
        # No excluded normalized text crosses the embedding boundary.
        excluded = c.episodes[0].normalized_text
        assert all(excluded not in call.texts for call in runtime.embedding.calls)


async def test_context_fallback_does_not_hide_search_error() -> None:
    class BrokenCall(FixtureEmbedding):
        async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            raise CoreError(502, "synthetic_failed", "synthetic")

    with isolated_case(config(), case("synonym-02"), BrokenCall(DATA)) as runtime:
        with pytest.raises(ValueError, match="context retrieval failed"):
            await observe(runtime)


async def test_changing_embedding_space_fails_closed() -> None:
    class ChangedSpace(FixtureEmbedding):
        async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            vectors = await super().embed(texts)
            self.space = replace(self.space, revision="changed")
            return vectors

    with isolated_case(config(), case("synonym-02"), ChangedSpace(DATA)) as runtime:
        with pytest.raises(ValueError, match="context retrieval failed"):
            await observe(runtime)


def test_required_tuning_cli_eighty_nine_cases_three_uncached_runs(tmp_path: Path) -> None:
    from digital_souls_core.semantic_evaluation_cases import load_evaluation_cases
    from digital_souls_core.semantic_retrieval_evaluation import case_version

    directory = ROOT / "evals/semantic/tuning"
    cases_path, gold_path = directory / "cases.json", directory / "expectations.json"
    data = load_evaluation_cases(cases_path, gold_path)
    output = tmp_path / "tuning-report.json"
    # The 267 extra schemas use their own disposable server: catalog/WAL growth
    # must not consume the storage suite's bounded 512 MiB PostgreSQL container.
    process = subprocess.run(
        [
            "bash",
            str(ROOT / "tools/with-test-postgres.sh"),
            sys.executable,
            "-I",
            str(ROOT / "tools/evaluate-semantic-retrieval.py"),
            "--cases",
            str(cases_path),
            "--expectations",
            str(gold_path),
            "--runs",
            "3",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=360,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout == process.stderr == ""
    serialized = output.read_text()
    report = EvaluationReport.model_validate_json(serialized)
    validate_report(report, data, 3)
    assert report.passed and report.mode == "fixture" and not report.quality_evidence
    assert report.case_version == case_version(data, cases_path, gold_path)
    assert report.case_version != case_version(
        DATA, ROOT / "evals/semantic/cases.json", ROOT / "evals/semantic/expectations.json"
    )
    assert all(len(run.cases) == 89 and run.gates_passed for run in report.runs)
    assert all(summary.rate == 1.0 for run in report.runs for summary in run.categories.values())
    assert all(row.passed for run in report.runs for row in run.cases)
    # epoch-change has no eligible records after revocation; every other case
    # embeds fresh inputs once per run, including the same logical record IDs.
    assert report.embedding_call_count == 3 * 88
    for c in data.cases.cases:
        assert c.query not in serialized
        assert all(r.normalized_text not in serialized for r in c.episodes)


def test_candidate_tuning_fixture_repeats_without_quality_claims(tmp_path: Path) -> None:
    # Separate PG process keeps the 267 schemas out of the storage suite's DB.
    script = """
import asyncio, json, os
from pathlib import Path
from digital_souls_core.postgres_db import PostgresConfig
from digital_souls_core.semantic_embedding_candidates import (
    PrefixEmbedding, evaluate_candidate, tuning_paths,
)
from digital_souls_core.semantic_evaluation_cases import load_evaluation_cases
from digital_souls_core.semantic_evaluation_runtime import FixtureEmbedding
from digital_souls_core.semantic_retrieval_evaluation import case_version, execution_commit
root = Path.cwd()
paths = tuning_paths(root)
data = load_evaluation_cases(*paths)
config = PostgresConfig(
    host=os.environ['DSC_TEST_POSTGRES_SOCKET'],
    port=int(os.environ['DSC_TEST_POSTGRES_PORT']),
    database=os.environ['DSC_TEST_POSTGRES_DATABASE'],
    user=os.environ['DSC_TEST_POSTGRES_USER'],
)
report = asyncio.run(evaluate_candidate(
    data, config, PrefixEmbedding(FixtureEmbedding(data)), runs=3,
    mode='fixture', commit=execution_commit(root), version=case_version(data, *paths),
))
Path(os.environ['CANDIDATE_TEST_OUTPUT']).write_text(json.dumps(report))
"""
    output = tmp_path / "candidate-report.json"
    process = subprocess.run(
        ["bash", str(ROOT / "tools/with-test-postgres.sh"), sys.executable, "-c", script],
        cwd=ROOT,
        env={**os.environ, "CANDIDATE_TEST_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        timeout=360,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    import json

    report = json.loads(output.read_text())
    assert report["baseline"]["passed"]
    assert not report["baseline"]["quality_evidence"]
    assert report["baseline"]["embedding_call_count"] == 264
    assert all(len(a["cases"]) == 89 and len(a["sweep"]) == 41 for a in report["analysis"])
    assert all(
        next(s for s in a["sweep"] if s["threshold"] == 0.54)["categories"]["cross_language"][
            "quality_passed"
        ]
        == 24
        for a in report["analysis"]
    )
    from digital_souls_core.semantic_embedding_candidates import tuning_paths
    from digital_souls_core.semantic_evaluation_cases import load_evaluation_cases

    data = load_evaluation_cases(*tuning_paths(ROOT))
    for case in data.cases.cases:
        assert case.query not in output.read_text()
        assert all(
            r.normalized_text not in output.read_text()
            for r in (*case.episodes, *case.facts, *case.semantics)
        )
    assert "vectors" not in output.read_text()

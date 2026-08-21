from __future__ import annotations

import json
from pathlib import Path

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[2]
MANIFEST_PATH = BACKEND_DIR / "evals" / "rag_retrieval" / "manifest.json"

REQUIRED_COVERAGE = {
    "memory_type:EPISODIC_EVENT",
    "memory_type:USER_PREFERENCE",
    "memory_type:INTERACTION_PREFERENCE",
    "negative:near_irrelevant",
    "negative:wrong_period",
    "boundary:other_character",
    "ranking:relevance_over_mention",
    "ranking:mention_tie_break",
    "ranking:null_mention",
    "sqlite:deleted",
    "sqlite:inactive",
    "sqlite:expired",
    "sqlite:old_policy",
    "sqlite:non_core_provider",
    "chroma:orphan",
    "privacy:api_key",
    "privacy:health",
    "privacy:financial",
    "privacy:address",
    "privacy:third_party",
    "privacy:abstain",
    "query:skip_sensitive",
    "failure:no_rag_fallback",
}


def _manifest() -> dict[str, object]:
    loaded: object = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write_manifest(
    path: Path,
    *,
    candidate_pool_size: int,
    expected_ids: list[str],
    candidates: list[dict[str, object]],
    failure: str | None = None,
) -> Path:
    case: dict[str, object] = {
        "case_id": "minimal",
        "coverage": [],
        "query": "query",
        "query_embedding": [0.0],
        "search_expected": True,
        "expected_ids": expected_ids,
        "candidates": candidates,
    }
    if failure is not None:
        case["failure"] = failure
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evaluated_at": "2026-08-21T00:00:00+00:00",
                "policy_version": "current",
                "ranking": {
                    "candidate_pool_size": candidate_pool_size,
                    "max_retrieved_memories": 5,
                    "relevance_threshold": 0.0,
                    "equivalence_margin": 0.0,
                },
                "cases": [case],
            }
        ),
        encoding="utf-8",
    )
    return path


def _candidate(
    memory_id: str, embedding: float, *, provider_id: str = "core"
) -> dict[str, object]:
    return {
        "id": memory_id,
        "text": memory_id,
        "memory_type": "EPISODIC_EVENT",
        "embedding": [embedding],
        "character_id": "miori",
        "provider_id": provider_id,
        "status": "ACTIVE",
        "expires_at": None,
        "policy_version": "current",
        "persisted": True,
        "privacy_safe": True,
        "last_user_mentioned_at": None,
        "created_at": "2026-08-01T00:00:00+00:00",
    }


def test_synthetic_manifest_covers_required_ranking_and_safety_cases() -> None:
    manifest = _manifest()
    cases = manifest.get("cases")
    assert isinstance(cases, list)
    assert cases
    case_ids = [case.get("case_id") for case in cases if isinstance(case, dict)]
    assert len(case_ids) == len(cases)
    assert all(isinstance(case_id, str) and case_id for case_id in case_ids)
    assert len(set(case_ids)) == len(case_ids)
    covered = {
        tag
        for case in cases
        if isinstance(case, dict)
        for tag in case.get("coverage", [])
        if isinstance(tag, str)
    }
    assert REQUIRED_COVERAGE <= covered


def test_synthetic_manifest_declares_reproducible_fixed_embeddings() -> None:
    manifest = _manifest()
    embedding = manifest.get("embedding")
    assert isinstance(embedding, dict)
    assert embedding.get("distance_metric") == "squared_l2"
    assert isinstance(embedding.get("model"), str) and embedding["model"]
    digest = embedding.get("digest")
    assert isinstance(digest, str) and digest.startswith("sha256:")
    cases = manifest.get("cases")
    assert isinstance(cases, list)
    searchable = [
        case
        for case in cases
        if isinstance(case, dict) and case.get("search_expected") is True
    ]
    assert searchable
    assert all(
        isinstance(case.get("query_embedding"), list)
        and case["query_embedding"]
        for case in searchable
    )


def test_deterministic_manifest_evaluation_meets_quality_contract() -> None:
    from evals.rag_retrieval.evaluator import evaluate_manifest

    result = evaluate_manifest(MANIFEST_PATH)

    assert result.privacy_boundary_violations == 0
    assert result.character_boundary_violations == 0
    assert result.threshold_violations == 0
    assert result.unverified_fallbacks == 0
    assert result.tie_break_accuracy == 1.0
    assert result.recall >= 0.8


def test_deterministic_evaluation_applies_candidate_pool_before_ranking(
    tmp_path: Path,
) -> None:
    from evals.rag_retrieval.evaluator import evaluate_manifest

    path = _write_manifest(
        tmp_path / "candidate-pool.json",
        candidate_pool_size=2,
        expected_ids=["first", "last-kept"],
        candidates=[
            _candidate("first", 0.1),
            _candidate("last-kept", 0.2),
            _candidate("first-excluded", 0.3),
        ],
    )

    result = evaluate_manifest(path)

    assert result.recall == 1.0
    assert result.precision == 1.0
    assert result.retrieved_items == 2


def test_evaluation_revalidates_core_provider_before_ranking(tmp_path: Path) -> None:
    from evals.rag_retrieval.evaluator import evaluate_manifest

    path = _write_manifest(
        tmp_path / "provider.json",
        candidate_pool_size=2,
        expected_ids=["core"],
        candidates=[
            _candidate("non-core", 0.01, provider_id="external"),
            _candidate("core", 0.1),
        ],
    )

    result = evaluate_manifest(path)

    assert result.recall == 1.0
    assert result.precision == 1.0
    assert result.retrieved_items == 1


def test_declared_search_failure_calls_provider_and_ends_without_fallback(
    tmp_path: Path,
) -> None:
    from evals.rag_retrieval.evaluator import SearchBoundaryFailure, evaluate_manifest

    path = _write_manifest(
        tmp_path / "failure.json",
        candidate_pool_size=7,
        expected_ids=[],
        candidates=[_candidate("unverified", 0.0) | {"persisted": False}],
        failure="chroma",
    )
    observed_pool_sizes: list[int] = []

    def failing_provider(
        _case: dict[str, object],
        _candidates: list[dict[str, object]],
        _query: tuple[float, ...],
        candidate_pool_size: int,
    ) -> dict[str, float]:
        observed_pool_sizes.append(candidate_pool_size)
        raise SearchBoundaryFailure("injected")

    result = evaluate_manifest(path, candidate_distance_provider=failing_provider)

    assert observed_pool_sizes == [7]
    assert result.retrieved_items == 0
    assert result.unverified_fallbacks == 0


def test_undeclared_search_failure_propagates(tmp_path: Path) -> None:
    from evals.rag_retrieval.evaluator import SearchBoundaryFailure, evaluate_manifest

    path = _write_manifest(
        tmp_path / "unexpected-failure.json",
        candidate_pool_size=3,
        expected_ids=[],
        candidates=[],
    )

    def failing_provider(
        _case: dict[str, object],
        _candidates: list[dict[str, object]],
        _query: tuple[float, ...],
        _candidate_pool_size: int,
    ) -> dict[str, float]:
        raise SearchBoundaryFailure("unexpected")

    with pytest.raises(SearchBoundaryFailure, match="unexpected"):
        evaluate_manifest(path, candidate_distance_provider=failing_provider)


def test_real_evaluator_passes_manifest_candidate_pool_to_chroma(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.memory.chroma_store import MemorySearchCandidate
    from app.runtime_paths import RuntimePaths
    from evals.rag_retrieval import real_evaluator

    path = _write_manifest(
        tmp_path / "real-provider.json",
        candidate_pool_size=7,
        expected_ids=["core"],
        candidates=[_candidate("core", 0.1)],
    )
    observed_n_results: list[int] = []

    def query_memories(
        _character_id: str,
        _embedding: list[float],
        n_results: int,
        *,
        chroma_path: Path,
    ) -> list[MemorySearchCandidate]:
        assert chroma_path == tmp_path / "chroma"
        observed_n_results.append(n_results)
        return [MemorySearchCandidate(memory_id="core", raw_distance=0.01)]

    monkeypatch.setattr(real_evaluator, "resolve_ollama_embedding_model", lambda: "model")
    monkeypatch.setattr(real_evaluator, "embed_text", lambda _text: [0.0])
    monkeypatch.setattr(real_evaluator, "upsert_memory_index_entry", lambda **_kwargs: None)
    monkeypatch.setattr(real_evaluator, "delete_memory_index_entry", lambda **_kwargs: None)
    monkeypatch.setattr(real_evaluator, "query_memories", query_memories)
    runtime_paths = RuntimePaths(
        environment_id="test",
        data_root=tmp_path,
        sqlite_path=tmp_path / "history.db",
        persona_memory_sqlite_path=tmp_path / "memory.db",
        chroma_path=tmp_path / "chroma",
        runtime_report_dir=tmp_path / "runtime",
        cache_path=tmp_path / "cache",
        whisper_cache_path=tmp_path / "cache" / "whisper",
        identity_marker_path=tmp_path / "identity.json",
        restore_intent_path=tmp_path / "restore.json",
    )

    result = real_evaluator.evaluate_real_manifest(
        path, runtime_paths=runtime_paths, embedding_model="model"
    )

    assert observed_n_results == [7]
    assert result.recall == 1.0

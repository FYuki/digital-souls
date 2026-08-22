from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.llm.ollama_config import resolve_ollama_embedding_model
from app.memory.chroma_store import (
    delete_memory_index_collection,
    delete_memory_index_entry,
    query_memories,
    upsert_memory_index_entry,
)
from app.memory.embedder import embed_text
from app.runtime_paths import RuntimePaths
from evals.rag_retrieval.evaluator import (
    EvaluationResult,
    SearchBoundaryFailure,
    evaluate_manifest,
)


def evaluate_real_manifest(
    path: Path,
    *,
    runtime_paths: RuntimePaths,
    embedding_model: str,
) -> EvaluationResult:
    if embedding_model != resolve_ollama_embedding_model():
        raise ValueError("embedding_model must match the resolved Ollama model")

    def real_distances(
        case: dict[str, object],
        candidates: list[dict[str, object]],
        _query_embedding: tuple[float, ...],
        candidate_pool_size: int,
    ) -> dict[str, float]:
        if case.get("failure") == "chroma":
            raise SearchBoundaryFailure("injected Chroma search failure")
        character_id = f"rag-eval-{uuid4().hex}"
        collection_started = False
        indexed_ids: list[str] = []
        try:
            for candidate in candidates:
                memory_id = _required_string(candidate.get("id"), "candidate.id")
                text = _required_string(candidate.get("text"), "candidate.text")
                embedding = embed_text(text)
                collection_started = True
                upsert_memory_index_entry(
                    character_id=character_id,
                    memory_id=memory_id,
                    embedding=embedding,
                    normalized_text=text,
                    provider_id="core",
                    memory_kind="persona",
                    memory_type=_required_string(
                        candidate.get("memory_type"), "candidate.memory_type"
                    ),
                    policy_version=_required_string(
                        candidate.get("policy_version"), "candidate.policy_version"
                    ),
                    occurred_at=_required_string(
                        candidate.get("created_at"), "candidate.created_at"
                    ),
                    expires_at=None,
                    chroma_path=runtime_paths.chroma_path,
                )
                indexed_ids.append(memory_id)
            query = _required_string(case.get("query"), "case.query")
            query_embedding = embed_text(query)
            collection_started = True
            results = query_memories(
                character_id,
                query_embedding,
                n_results=candidate_pool_size,
                chroma_path=runtime_paths.chroma_path,
            )
            return {result.memory_id: result.raw_distance for result in results}
        finally:
            try:
                for memory_id in indexed_ids:
                    delete_memory_index_entry(
                        character_id=character_id,
                        memory_id=memory_id,
                        chroma_path=runtime_paths.chroma_path,
                    )
            finally:
                if collection_started:
                    delete_memory_index_collection(
                        character_id=character_id,
                        chroma_path=runtime_paths.chroma_path,
                    )

    return evaluate_manifest(path, candidate_distance_provider=real_distances)


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value

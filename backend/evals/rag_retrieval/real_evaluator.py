from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.inference.runtime import create_inference_runtime
from app.memory.chroma_store import (
    EmbeddingFingerprint,
    activate_memory_index,
    delete_memory_index_collection,
    delete_memory_index_entry,
    query_memories,
    upsert_memory_index_entry,
)
from app.memory.inference_client import MemoryInferenceEmbedder
from app.runtime_paths import RuntimePaths
from evals.rag_retrieval.evaluator import (
    EvaluationResult,
    SearchBoundaryFailure,
    evaluate_manifest,
)


class _Embedder(Protocol):
    provider_id: str
    model_id: str

    def __call__(self, text: str) -> list[float]: ...


def evaluate_real_manifest(
    path: Path,
    *,
    runtime_paths: RuntimePaths,
    embedding_model: str,
    embedder: _Embedder | None = None,
) -> EvaluationResult:
    inference_runtime = None
    if embedder is None:
        inference_runtime = create_inference_runtime(os.environ)
        embedder = MemoryInferenceEmbedder(
            router=inference_runtime.router,
            settings=inference_runtime.settings,
        )
    if embedding_model != embedder.model_id:
        if inference_runtime is not None:
            inference_runtime.close()
        raise ValueError("embedding_model must match the resolved Inference target")

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
                if "occurred_at" not in candidate:
                    raise ValueError("candidate.occurred_at is required")
                embedding = embedder(text)
                fingerprint = EmbeddingFingerprint(
                    embedder.provider_id, embedder.model_id, len(embedding)
                )
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
                    occurred_at=_optional_string(
                        candidate["occurred_at"], "candidate.occurred_at"
                    ),
                    expires_at=None,
                    chroma_path=runtime_paths.chroma_path,
                    fingerprint=fingerprint,
                )
                indexed_ids.append(memory_id)
            query = _required_string(case.get("query"), "case.query")
            query_embedding = embedder(query)
            fingerprint = EmbeddingFingerprint(
                embedder.provider_id, embedder.model_id, len(query_embedding)
            )
            activate_memory_index(character_id, fingerprint, runtime_paths.chroma_path)
            collection_started = True
            results = query_memories(
                character_id,
                query_embedding,
                n_results=candidate_pool_size,
                chroma_path=runtime_paths.chroma_path,
                fingerprint=fingerprint,
            )
            return {result.memory_id: result.raw_distance for result in results}
        finally:
            try:
                for memory_id in indexed_ids:
                    delete_memory_index_entry(
                        character_id=character_id,
                        memory_id=memory_id,
                        chroma_path=runtime_paths.chroma_path,
                        fingerprint=fingerprint,
                    )
            finally:
                if collection_started:
                    delete_memory_index_collection(
                        character_id=character_id,
                        chroma_path=runtime_paths.chroma_path,
                        fingerprint=fingerprint,
                    )

    try:
        return evaluate_manifest(path, candidate_distance_provider=real_distances)
    finally:
        if inference_runtime is not None:
            inference_runtime.close()


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, label)

import importlib
from uuid import uuid4

import httpx
import pytest


def _require_runtime_evidence_dependencies() -> None:
    importlib.import_module("chromadb")

    from app.llm.ollama_config import (
        resolve_ollama_base_url,
        resolve_ollama_embedding_model,
    )

    response = httpx.get(f"{resolve_ollama_base_url()}/api/tags", timeout=5.0)
    response.raise_for_status()
    models = response.json().get("models")
    if not isinstance(models, list):
        pytest.fail("Ollama tags response does not include models")
    model_name = resolve_ollama_embedding_model()
    available = {
        model.get("name")
        for model in models
        if isinstance(model, dict) and isinstance(model.get("name"), str)
    }
    if model_name not in available:
        pytest.fail(f"Ollama model is not pulled: {model_name}")


class TestRagRuntimeEvidenceIntegration:
    def test_rebuildable_index_remains_available_to_the_retrieval_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        runtime_paths,
    ) -> None:
        monkeypatch.setenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text:latest")
        _require_runtime_evidence_dependencies()

        from app.memory.chroma_store import add_memory
        from app.memory.embedder import embed_text
        from app.memory.memory_policy import resolved_memory_policy
        from app.memory.rag_service import retrieve_prompt_memories

        character_id = f"miori{uuid4().hex[:8]}"
        content = "ユーザーは紅茶を好む。"
        add_memory(
            character_id,
            str(uuid4()),
            embed_text(content),
            content,
            {
                "character": character_id,
                "role": "user",
                "timestamp": "2026-08-20T00:00:00+00:00",
            },
            chroma_path=runtime_paths.chroma_path,
        )

        memories = retrieve_prompt_memories(
            character_id,
            "紅茶の好みは？",
            resolved_memory_policy(),
            chroma_path=runtime_paths.chroma_path,
        )

        assert any(memory.content == content for memory in memories)

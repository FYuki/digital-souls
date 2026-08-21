import importlib
from dataclasses import replace
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID
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

        from app.memory.index_sync import MemoryIndexSync
        from app.memory.embedder import embed_text
        from app.memory.chroma_store import query_memories, upsert_memory_index_entry
        from app.memory.memory_policy import resolved_memory_policy
        from app.memory.persistence.approved_repository import ApprovedMemoryRepository
        from app.memory.persistence.index_outbox_repository import IndexOutboxRepository
        from app.memory.persistence.schema import initialize_persona_memory_schema
        from app.memory.rag_service import retrieve_prompt_memories
        from app.privacy.scanner import create_privacy_scanner
        from app.privacy.semantic.contracts import (
            PrivacyAssessment,
            SemanticAssessmentReasonCode,
            SemanticClassification,
            SemanticPrivacyCategory,
            SubjectScope,
        )
        from tests.unit.test_approved_memory_repository import _candidate, _context

        importlib.import_module("app.backup_restore.service")
        character_id = f"miori{uuid4().hex[:8]}"
        content = "ユーザーは紅茶を好む。"
        repository_root = Path(__file__).resolve().parents[2]
        initialize_persona_memory_schema(runtime_paths, repository_root)
        ids = count(1)
        approved = ApprovedMemoryRepository(
            database_path=runtime_paths.persona_memory_sqlite_path,
            clock=lambda: datetime(2026, 8, 20, tzinfo=UTC),
            uuid_factory=lambda: UUID(
                f"00000000-0000-4000-8000-{next(ids):012d}"
            ),
            outbox_uuid_factory=lambda: UUID(
                f"10000000-0000-4000-8000-{next(ids):012d}"
            ),
        )
        policy = resolved_memory_policy()
        saved = approved.save(
            character_id=character_id,
            candidate=_candidate(content),
            context=replace(_context(), policy_version=policy.policy_version),
        )
        sync = MemoryIndexSync(
            approved_repository=approved,
            outbox_repository=IndexOutboxRepository(
                database_path=runtime_paths.persona_memory_sqlite_path,
                clock=lambda: datetime(2026, 8, 20, tzinfo=UTC),
            ),
            chroma_path=runtime_paths.chroma_path,
            runtime_report_dir=runtime_paths.runtime_report_dir,
            embedder=embed_text,
            clock=lambda: datetime(2026, 8, 20, tzinfo=UTC),
        )
        assert not runtime_paths.chroma_path.exists()

        sync.reconcile_once()

        query_embedding = embed_text("紅茶の好みは？")
        upsert_memory_index_entry(
            character_id=character_id,
            memory_id=str(saved.id),
            embedding=query_embedding,
            normalized_text="改ざんされたChroma本文",
            provider_id=saved.provider_id,
            memory_kind=saved.memory_kind,
            memory_type=saved.memory_type.value,
            policy_version=saved.policy_version,
            effective_at=saved.effective_at.isoformat(),
            expires_at=None,
            chroma_path=runtime_paths.chroma_path,
        )
        indexed = query_memories(
            character_id,
            query_embedding,
            n_results=5,
            chroma_path=runtime_paths.chroma_path,
        )
        assert len(indexed) == 1
        classifier = MagicMock()
        classifier.classify.return_value = PrivacyAssessment(
            classification=SemanticClassification.NOT_SENSITIVE,
            subject_scope=SubjectScope.GENERAL,
            category=SemanticPrivacyCategory.NONE,
            reason_code=SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT,
            classifier_version="integration-fake-v1",
            model_id="integration-fake",
            model_digest="sha256:integration-fake",
            prompt_version="integration-fake-v1",
            policy_version=policy.policy_version,
        )

        memories = retrieve_prompt_memories(
            character_id,
            "紅茶の好みは？",
            policy,
            scanner=create_privacy_scanner(policy.privacy),
            classifier=classifier,
            approved_repository=approved,
            chroma_path=runtime_paths.chroma_path,
        )

        assert [(memory.normalized_text, memory.raw_distance) for memory in memories] == [
            (content, indexed[0].raw_distance)
        ]

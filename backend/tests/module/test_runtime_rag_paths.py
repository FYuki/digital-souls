from __future__ import annotations

from unittest.mock import MagicMock
from datetime import UTC, datetime

from app.privacy.contracts import ScanSuccess
from app.privacy.semantic.contracts import (
    PrivacyAssessment,
    SemanticAssessmentReasonCode,
    SemanticClassification,
    SemanticPrivacyCategory,
    SubjectScope,
)


def test_rt_chroma_01_rag_lookup_uses_resolved_chroma_path(
    monkeypatch, tmp_path
) -> None:
    from app.memory import rag_service
    from app.memory.memory_policy import resolved_memory_policy

    chroma_path = tmp_path / "runtime-data" / "chroma"
    embedder = MagicMock(return_value=[0.5])
    monkeypatch.setattr(rag_service, "query_memories", MagicMock(return_value=[]))
    scanner = MagicMock()
    scanner.scan.return_value = ScanSuccess(())
    classifier = MagicMock()
    classifier.classify.return_value = PrivacyAssessment(
        classification=SemanticClassification.NOT_SENSITIVE,
        subject_scope=SubjectScope.GENERAL,
        category=SemanticPrivacyCategory.NONE,
        reason_code=SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT,
        classifier_version="classifier-v1",
        model_id="model-v1",
        model_digest="sha256:test",
        prompt_version="prompt-v1",
        policy_version=resolved_memory_policy().policy_version,
    )

    rag_service.retrieve_prompt_memories(
        "miori",
        "前回の畑の話を教えて",
        resolved_memory_policy(),
        scanner=scanner,
        classifier=classifier,
        approved_repository=MagicMock(),
        embedder=embedder,
        chroma_path=chroma_path,
        now=datetime(2026, 8, 20, tzinfo=UTC),
        timezone="Asia/Tokyo",
    )

    rag_service.query_memories.assert_called_once_with(
        "miori",
        [0.5],
        n_results=20,
        chroma_path=chroma_path,
        fingerprint=None,
    )

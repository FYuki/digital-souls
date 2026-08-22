from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.memory.admission.contracts import (
    ApprovedMemoryCandidate,
    MemoryCandidate,
    MemoryType,
    PreferencePolarity,
    RagAdmissionDecision,
    RagAdmissionResult,
    UserPreferenceValue,
)
from app.memory.persistence.contracts import (
    FormationMethod,
    MemorySourceType,
)
from app.privacy.semantic.contracts import (
    PrivacyAssessment,
    SemanticAssessmentReasonCode,
    SemanticClassification,
    SemanticPrivacyCategory,
    SubjectScope,
)
from tests.unit.test_memory_rag_service import _approved_memory


MEMORY_ID = UUID("00000000-0000-4000-8000-000000000012")
OPERATION_ID = UUID("10000000-0000-4000-8000-000000000012")
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _candidate(secret: str = "紅茶") -> MemoryCandidate:
    return MemoryCandidate(
        memory_type=MemoryType.USER_PREFERENCE,
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object=secret,
        ),
        source=None,
    )


def _assessment() -> PrivacyAssessment:
    return PrivacyAssessment(
        classification=SemanticClassification.NOT_SENSITIVE,
        subject_scope=SubjectScope.SELF,
        category=SemanticPrivacyCategory.NONE,
        reason_code=SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT,
        classifier_version="classifier-v12",
        model_id="gemma4:e4b",
        model_digest="sha256:model-v12",
        prompt_version="classifier-prompt-v12",
        policy_version="policy-v12",
    )


def _provider(*, result: RagAdmissionResult, assessment: PrivacyAssessment):
    from app.memory.providers import PersonaMemoryProvider

    repository = MagicMock()
    repository.get.return_value = _approved_memory(id=MEMORY_ID)
    repository.correct.return_value = _approved_memory(
        id=MEMORY_ID,
        normalized_text="ユーザーは紅茶が好き",
        content_version=2,
    )
    scanner = MagicMock()
    classifier = MagicMock()
    classifier.classify.return_value = assessment
    evaluator = MagicMock()
    evaluator.evaluate_manual_correction.return_value = result
    index_sync = MagicMock()
    provider = PersonaMemoryProvider(
        approved_repository=repository,
        scanner=scanner,
        classifier=classifier,
        admission_evaluator=evaluator,
        index_sync=index_sync,
        clock=lambda: NOW,
    )
    return provider, repository, index_sync


def test_allowed_persona_correction_uses_existing_receipt_and_manual_context() -> None:
    assessment = _assessment()
    approved = ApprovedMemoryCandidate(
        structured_value=_candidate().structured_value,
        normalized_text="ユーザーは紅茶が好き",
    )
    provider, repository, _index_sync = _provider(
        result=RagAdmissionResult(RagAdmissionDecision.ALLOW_STRUCTURED, approved),
        assessment=assessment,
    )

    corrected = provider.correct(
        character_id="miori",
        memory_id=MEMORY_ID,
        candidate=_candidate(),
        idempotency_key=OPERATION_ID,
    )

    assert corrected.content_version == 2
    context = repository.correct.call_args.kwargs["context"]
    assert context.formation_method is FormationMethod.DIRECT
    assert context.idempotency_key == str(OPERATION_ID)
    assert context.policy_version == assessment.policy_version
    assert context.classifier_version == assessment.classifier_version
    assert context.model_id == assessment.model_id
    assert context.model_digest == assessment.model_digest
    assert context.prompt_version == "manual-correction"
    assert context.sources[0].source_type is MemorySourceType.USER_CORRECTION
    assert context.sources[0].source_provider_id == "core"
    assert context.sources[0].source_ref == str(OPERATION_ID)


@pytest.mark.parametrize(
    "decision",
    [
        RagAdmissionDecision.DENY_SENSITIVE,
        RagAdmissionDecision.DENY_USER_REQUEST,
        RagAdmissionDecision.ABSTAIN_UNKNOWN,
        RagAdmissionDecision.NOT_MEMORY_WORTHY,
    ],
)
def test_rejected_persona_correction_returns_only_reason_and_changes_nothing(
    decision: RagAdmissionDecision,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.memory.providers import MemoryCorrectionRejected

    marker = "REJECTED_PERSONA_SECRET_12"
    provider, repository, index_sync = _provider(
        result=RagAdmissionResult(decision, None),
        assessment=_assessment(),
    )

    with pytest.raises(MemoryCorrectionRejected) as captured:
        provider.correct(
            character_id="miori",
            memory_id=MEMORY_ID,
            candidate=_candidate(marker),
            idempotency_key=OPERATION_ID,
        )

    assert captured.value.reason_code == decision.value
    assert marker not in str(captured.value)
    assert marker not in caplog.text
    repository.correct.assert_not_called()
    repository.hard_delete.assert_not_called()
    index_sync.delete_after_commit.assert_not_called()

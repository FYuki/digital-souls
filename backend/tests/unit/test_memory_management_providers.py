from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi.encoders import jsonable_encoder

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
    ApprovedMemoryDetail,
    FormationMethod,
    MemorySourceInput,
    MemorySourceType,
)
from app.privacy.semantic.contracts import (
    PrivacyAssessment,
    SemanticAssessmentReasonCode,
    SemanticClassification,
    SemanticPrivacyCategory,
    SubjectScope,
)
from tests.unit._helpers import approved_memory


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
    repository.get.return_value = approved_memory(id=MEMORY_ID)
    repository.correct.return_value = approved_memory(
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
    current = repository.get.return_value
    assert context.occurred_at == current.occurred_at
    assert context.occurred_timezone == current.occurred_timezone
    assert context.occurred_precision is current.occurred_precision
    assert context.stated_at == current.stated_at
    assert context.expires_at == current.expires_at
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


def test_persona_list_uses_bulk_details_and_skips_concurrently_deleted_memory() -> None:
    approved = approved_memory(id=MEMORY_ID)
    deleted_id = UUID("00000000-0000-4000-8000-000000000013")
    deleted = approved_memory(id=deleted_id)
    provider, repository, _index_sync = _provider(
        result=RagAdmissionResult(RagAdmissionDecision.ABSTAIN_UNKNOWN, None),
        assessment=_assessment(),
    )
    repository.list_by_provider.return_value = [approved, deleted]
    repository.get_details.return_value = {
        MEMORY_ID: ApprovedMemoryDetail(approved, (), ())
    }
    repository.pending_index_memory_ids.return_value = frozenset({MEMORY_ID})

    results = provider.list(character_id="miori", status="ACTIVE")

    assert [result["id"] for result in results] == [MEMORY_ID]
    assert results[0]["index_pending"] is True
    repository.get_detail.assert_not_called()
    repository.is_index_pending.assert_not_called()
    repository.get_details.assert_called_once_with(
        character_id="miori",
        provider_id="core",
        memory_ids=(MEMORY_ID, deleted_id),
    )
    repository.pending_index_memory_ids.assert_called_once_with(
        character_id="miori", memory_ids=(MEMORY_ID,)
    )


def test_persona_get_preserves_consolidation_source_for_management_serialization() -> (
    None
):
    source_memory_id = UUID("00000000-0000-4000-8000-000000000014")
    source = MemorySourceInput(
        source_type=MemorySourceType.CONSOLIDATION,
        source_provider_id="core",
        source_ref=str(source_memory_id),
    )
    memory = approved_memory(id=MEMORY_ID)
    provider, repository, _index_sync = _provider(
        result=RagAdmissionResult(RagAdmissionDecision.ABSTAIN_UNKNOWN, None),
        assessment=_assessment(),
    )
    repository.get_detail.return_value = ApprovedMemoryDetail(memory, (source,), ())
    repository.is_index_pending.return_value = False

    result = provider.get(character_id="miori", memory_id=MEMORY_ID)

    assert result is not None
    sources = result["sources"]
    assert isinstance(sources, tuple)
    assert sources == (source,)
    returned_source = sources[0]
    assert isinstance(returned_source, MemorySourceInput)
    serialized = jsonable_encoder(result)
    assert serialized["sources"] == [
        {
            "source_type": "CONSOLIDATION",
            "source_provider_id": "core",
            "source_ref": str(source_memory_id),
        }
    ]

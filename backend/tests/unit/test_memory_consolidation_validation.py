from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.memory.admission.contracts import (
    ApprovedMemoryCandidate,
    InteractionAspect,
    InteractionPreferenceValue,
    MemoryType,
    PreferencePolarity,
    UserPreferenceValue,
)
from app.memory.persistence.contracts import MemoryStatus
from app.privacy.semantic.contracts import (
    PrivacyAssessment,
    SemanticAssessmentReasonCode,
    SemanticClassification,
    SemanticPrivacyCategory,
    SubjectScope,
)
from tests.unit._helpers import approved_memory


MEMORY_ONE = UUID("00000000-0000-4000-8000-000000000001")
MEMORY_TWO = UUID("00000000-0000-4000-8000-000000000002")
MEMORY_THREE = UUID("00000000-0000-4000-8000-000000000003")
MEMORY_FOUR = UUID("00000000-0000-4000-8000-000000000004")


def _source(memory_id: UUID):
    from app.memory.persistence.contracts import MemorySourceInput, MemorySourceType

    return MemorySourceInput(
        source_type=MemorySourceType.CONVERSATION_TURN,
        source_provider_id="core",
        source_ref=f"conversation:{memory_id}",
    )


def _detail(memory_id: UUID, **memory_overrides: object):
    from app.memory.persistence.contracts import ApprovedMemoryDetail

    memory = approved_memory(id=memory_id, **memory_overrides)
    return ApprovedMemoryDetail(
        memory=memory, sources=(_source(memory_id),), lineage=()
    )


def _plan(plan_type: str = "MERGE"):
    from app.memory.consolidation.contracts import MemoryVersionRef
    from app.memory.consolidation.planner import parse_consolidation_response

    extra: dict[str, object] = {}
    if plan_type in {"MERGE", "SUPERSEDE"}:
        extra = {
            "memory_type": "USER_PREFERENCE",
            "structured_value": {"polarity": "LIKE", "object": "紅茶"},
        }
    if plan_type == "DELETE_EXACT_DUPLICATE":
        extra = {"canonical_memory_id": str(MEMORY_ONE)}
    return parse_consolidation_response(
        json.dumps(
            {
                "plans": [
                    {
                        "plan_type": plan_type,
                        "reason_code": "MODEL_SELECTED",
                        "memories": [
                            {"memory_id": str(MEMORY_ONE), "content_version": 1},
                            {"memory_id": str(MEMORY_TWO), "content_version": 1},
                        ],
                        **extra,
                    }
                ]
            }
        ),
        expected_memories=tuple(
            MemoryVersionRef(memory_id=memory_id, content_version=1)
            for memory_id in (MEMORY_ONE, MEMORY_TWO)
        ),
    ).plans[0]


@pytest.mark.parametrize(
    ("changed_index", "overrides", "reason_code"),
    (
        (0, {"character_id": "other"}, "CHARACTER_BOUNDARY"),
        (0, {"provider_id": "temporary:recipe"}, "PROVIDER_BOUNDARY"),
        (0, {"memory_kind": "EPISODIC"}, "MEMORY_TYPE_BOUNDARY"),
        (0, {"content_version": 2}, "VERSION_CONFLICT"),
        (0, {"status": MemoryStatus.INACTIVE}, "STATUS_CONFLICT"),
    ),
)
def test_validation_turns_authoritative_boundary_changes_into_noop(
    changed_index: int,
    overrides: dict[str, object],
    reason_code: str,
) -> None:
    from app.memory.consolidation.contracts import ConsolidationPlanType
    from app.memory.consolidation.validation import validate_plan

    planned = (_detail(MEMORY_ONE), _detail(MEMORY_TWO))
    current = list(planned)
    current[changed_index] = replace(
        current[changed_index],
        memory=replace(current[changed_index].memory, **overrides),
    )

    outcome = validate_plan(
        plan=_plan(),
        planned=planned,
        current=tuple(current),
    )

    assert outcome.plan_type is ConsolidationPlanType.NOOP
    assert outcome.reason_code == reason_code


@pytest.mark.parametrize("changed_field", ("sources", "lineage"))
def test_validation_turns_source_or_lineage_snapshot_change_into_noop(
    changed_field: str,
) -> None:
    from app.memory.consolidation.contracts import ConsolidationPlanType
    from app.memory.persistence.contracts import (
        MemoryLineageInput,
        MemoryLineageRelation,
        MemorySourceInput,
        MemorySourceType,
    )
    from app.memory.consolidation.validation import validate_plan

    planned = (_detail(MEMORY_ONE), _detail(MEMORY_TWO))
    replacement = {
        "sources": (
            MemorySourceInput(
                source_type=MemorySourceType.CONVERSATION_TURN,
                source_provider_id="core",
                source_ref="changed-source",
            ),
        ),
        "lineage": (
            MemoryLineageInput(
                related_memory_id=MEMORY_TWO,
                relation=MemoryLineageRelation.DUPLICATE_OF,
            ),
        ),
    }[changed_field]
    current = (replace(planned[0], **{changed_field: replacement}), planned[1])

    outcome = validate_plan(plan=_plan(), planned=planned, current=current)

    assert outcome.plan_type is ConsolidationPlanType.NOOP
    assert outcome.reason_code == f"{changed_field.upper()}_CONFLICT"


def test_exact_duplicate_requires_every_persisted_content_and_temporal_value_to_match() -> (
    None
):
    from app.memory.consolidation.validation import is_exact_duplicate
    from app.memory.persistence.contracts import ApprovedMemoryDetail

    baseline = approved_memory(
        id=MEMORY_ONE,
        normalized_text="ユーザーは紅茶を好む",
        stated_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    exact = replace(
        baseline, id=MEMORY_TWO, created_at=datetime(2026, 8, 2, tzinfo=UTC)
    )
    paraphrase = replace(exact, normalized_text="紅茶が好き")
    different_time = replace(exact, stated_at=datetime(2026, 8, 3, tzinfo=UTC))
    different_structure = replace(
        exact,
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.DISLIKE,
            object="紅茶",
        ),
    )

    def detail(memory):
        return ApprovedMemoryDetail(memory=memory, sources=(), lineage=())

    assert is_exact_duplicate((detail(baseline), detail(exact))) is True
    assert is_exact_duplicate((detail(baseline), detail(paraphrase))) is False
    assert is_exact_duplicate((detail(baseline), detail(different_time))) is False
    assert is_exact_duplicate((detail(baseline), detail(different_structure))) is False


@pytest.mark.parametrize("changed_field", ("sources", "lineage"))
def test_exact_duplicate_requires_identical_provenance_and_lineage(
    changed_field: str,
) -> None:
    from app.memory.consolidation.validation import is_exact_duplicate
    from app.memory.persistence.contracts import (
        MemoryLineageInput,
        MemoryLineageRelation,
        MemorySourceInput,
        MemorySourceType,
    )

    baseline = _detail(MEMORY_ONE)
    exact = replace(baseline, memory=replace(baseline.memory, id=MEMORY_TWO))
    replacement = {
        "sources": (
            MemorySourceInput(
                source_type=MemorySourceType.CONVERSATION_TURN,
                source_provider_id="core",
                source_ref="different-source",
            ),
        ),
        "lineage": (
            MemoryLineageInput(
                related_memory_id=MEMORY_THREE,
                relation=MemoryLineageRelation.CONSOLIDATED_FROM,
            ),
        ),
    }[changed_field]

    assert is_exact_duplicate((baseline, exact)) is True
    assert is_exact_duplicate(
        (baseline, replace(exact, **{changed_field: replacement}))
    ) is False


def test_non_exact_duplicate_delete_plan_never_reaches_repository() -> None:
    from unittest.mock import Mock

    from app.memory.consolidation.contracts import ConsolidationPlanType
    from app.memory.consolidation.service import apply_validated_plan

    planned = (
        _detail(MEMORY_ONE, normalized_text="ユーザーは紅茶を好む"),
        _detail(MEMORY_TWO, normalized_text="紅茶が好き"),
    )
    repository = Mock()
    privacy_reviewer = Mock()

    outcome = apply_validated_plan(
        plan=_plan("DELETE_EXACT_DUPLICATE"),
        planned=planned,
        current=planned,
        repository=repository,
        privacy_reviewer=privacy_reviewer,
        consolidated_at=datetime(2026, 8, 23, tzinfo=UTC),
        model_id="gemma4:e4b",
        prompt_version="consolidation-v1",
        policy_version="policy-v1",
        deadline=1.0,
        monotonic_clock=lambda: 0.0,
    )

    assert outcome.plan_type is ConsolidationPlanType.NOOP
    repository.apply_consolidation.assert_not_called()
    privacy_reviewer.review.assert_not_called()


def test_candidate_batches_only_group_core_active_memories_by_character_and_type() -> (
    None
):
    from app.memory.consolidation.selection import build_candidate_batches

    core_miori = approved_memory(id=MEMORY_ONE)
    core_other = approved_memory(id=MEMORY_TWO, character_id="other")
    other_type = approved_memory(
        id=MEMORY_THREE,
        memory_type=MemoryType.INTERACTION_PREFERENCE,
        structured_value=InteractionPreferenceValue(
            aspect=InteractionAspect.LANGUAGE,
            value="日本語",
        ),
        normalized_text="ユーザーは日本語での応答を好む",
    )
    inactive = approved_memory(id=MEMORY_FOUR, status=MemoryStatus.INACTIVE)
    temporary = approved_memory(
        id=UUID("00000000-0000-4000-8000-000000000005"),
        provider_id="temporary:agriculture",
    )

    batches = build_candidate_batches(
        (temporary, inactive, other_type, core_other, core_miori),
        batch_size=10,
    )

    assert {tuple(memory.id for memory in batch) for batch in batches} == {
        (core_miori.id,),
        (core_other.id,),
        (other_type.id,),
    }
    assert inactive.id not in {memory.id for batch in batches for memory in batch}
    assert all(memory.provider_id == "core" for batch in batches for memory in batch)


def test_conflict_is_record_only_and_never_reaches_repository(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from unittest.mock import Mock

    from app.memory.consolidation.service import apply_validated_plan

    planned = (_detail(MEMORY_ONE), _detail(MEMORY_TWO))
    repository = Mock()
    privacy_reviewer = Mock()
    caplog.set_level(logging.INFO)

    outcome = apply_validated_plan(
        plan=_plan("CONFLICT"),
        planned=planned,
        current=planned,
        repository=repository,
        privacy_reviewer=privacy_reviewer,
        consolidated_at=datetime(2026, 8, 23, tzinfo=UTC),
        model_id="gemma4:e4b",
        prompt_version="consolidation-v1",
        policy_version="policy-v1",
        deadline=1.0,
        monotonic_clock=lambda: 0.0,
    )

    assert outcome.plan_type.value == "CONFLICT"
    repository.apply_consolidation.assert_not_called()
    privacy_reviewer.review.assert_not_called()
    observed = "\n".join(record.getMessage() for record in caplog.records)
    assert "CONFLICT" in observed
    assert "MODEL_SELECTED" in observed
    assert str(MEMORY_ONE) in observed
    assert "ユーザーは紅茶を好む" not in observed


def test_merge_rechecks_privacy_and_persists_assessment_provenance() -> None:
    from unittest.mock import Mock

    from app.memory.consolidation.contracts import ConsolidationPrivacyReview
    from app.memory.consolidation.service import apply_validated_plan

    planned = (_detail(MEMORY_ONE), _detail(MEMORY_TWO))
    assessment = PrivacyAssessment(
        classification=SemanticClassification.NOT_SENSITIVE,
        subject_scope=SubjectScope.SELF,
        category=SemanticPrivacyCategory.NONE,
        reason_code=SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT,
        classifier_version="privacy-classifier-v7",
        model_id="privacy-model",
        model_digest="privacy-digest",
        prompt_version="privacy-prompt-v3",
        policy_version="policy-v9",
    )
    reviewed_candidate = ApprovedMemoryCandidate(
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object="紅茶",
        ),
        normalized_text="ユーザーは紅茶を好む",
    )
    privacy_reviewer = Mock()
    privacy_reviewer.review.return_value = ConsolidationPrivacyReview(
        candidate=reviewed_candidate,
        assessment=assessment,
    )
    repository = Mock()

    apply_validated_plan(
        plan=_plan("MERGE"),
        planned=planned,
        current=planned,
        repository=repository,
        privacy_reviewer=privacy_reviewer,
        consolidated_at=datetime(2026, 8, 23, tzinfo=UTC),
        model_id="consolidation-model",
        prompt_version="consolidation-v1",
        policy_version="policy-v9",
        deadline=1.0,
        monotonic_clock=lambda: 0.0,
    )

    privacy_reviewer.review.assert_called_once()
    write = repository.apply_consolidation.call_args.kwargs
    assert write["candidate"] == reviewed_candidate
    assert write["context"].classifier_version == "privacy-classifier-v7"
    assert write["context"].model_id == "privacy-model"
    assert write["context"].model_digest == "privacy-digest"
    assert write["context"].policy_version == "policy-v9"
    assert write["context"].prompt_version == "consolidation-v1"


@pytest.mark.parametrize(
    "assessment",
    (
        PrivacyAssessment(
            classification=SemanticClassification.SENSITIVE,
            subject_scope=SubjectScope.SELF,
            category=SemanticPrivacyCategory.HEALTH,
            reason_code=SemanticAssessmentReasonCode.SENSITIVE_CONTENT,
            classifier_version="privacy-classifier-v7",
            model_id="privacy-model",
            model_digest="privacy-digest",
            prompt_version="privacy-prompt-v3",
            policy_version="policy-v9",
        ),
        PrivacyAssessment(
            classification=SemanticClassification.ABSTAIN,
            subject_scope=SubjectScope.UNKNOWN,
            category=SemanticPrivacyCategory.UNKNOWN,
            reason_code=SemanticAssessmentReasonCode.MODEL_UNAVAILABLE,
            classifier_version="privacy-classifier-v7",
            model_id="privacy-model",
            model_digest="privacy-digest",
            prompt_version="privacy-prompt-v3",
            policy_version="policy-v9",
        ),
    ),
)
def test_merge_privacy_rejection_or_abstention_becomes_noop(
    assessment: PrivacyAssessment,
) -> None:
    from unittest.mock import Mock

    from app.memory.consolidation.contracts import (
        ConsolidationPlanType,
        ConsolidationPrivacyReview,
    )
    from app.memory.consolidation.service import apply_validated_plan

    planned = (_detail(MEMORY_ONE), _detail(MEMORY_TWO))
    privacy_reviewer = Mock()
    privacy_reviewer.review.return_value = ConsolidationPrivacyReview(
        candidate=None,
        assessment=assessment,
    )
    repository = Mock()

    outcome = apply_validated_plan(
        plan=_plan("MERGE"),
        planned=planned,
        current=planned,
        repository=repository,
        privacy_reviewer=privacy_reviewer,
        consolidated_at=datetime(2026, 8, 23, tzinfo=UTC),
        model_id="consolidation-model",
        prompt_version="consolidation-v1",
        policy_version="policy-v9",
        deadline=1.0,
        monotonic_clock=lambda: 0.0,
    )

    assert outcome.plan_type is ConsolidationPlanType.NOOP
    privacy_reviewer.review.assert_called_once()
    repository.apply_consolidation.assert_not_called()


def test_privacy_review_caps_single_and_total_timeout_at_remaining_budget() -> None:
    from unittest.mock import Mock

    from app.memory.consolidation.privacy import ConsolidationPrivacyReviewer

    assessment = PrivacyAssessment(
        classification=SemanticClassification.NOT_SENSITIVE,
        subject_scope=SubjectScope.SELF,
        category=SemanticPrivacyCategory.NONE,
        reason_code=SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT,
        classifier_version="privacy-classifier-v7",
        model_id="privacy-model",
        model_digest="privacy-digest",
        prompt_version="privacy-prompt-v3",
        policy_version="policy-v9",
    )
    classifier = Mock()
    classifier.classify.return_value = assessment
    evaluator = Mock()
    evaluator.slot_values.return_value = {"object": "紅茶"}
    evaluator.evaluate_manual_correction.return_value = Mock(candidate=None)
    reviewer = ConsolidationPrivacyReviewer(
        scanner=Mock(),
        classifier=classifier,
        evaluator=evaluator,
    )

    reviewer.review(
        memory_type=MemoryType.USER_PREFERENCE,
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object="紅茶",
        ),
        timeout_seconds=0.25,
    )

    profile = classifier.classify.call_args.args[1]
    assert profile.timeout_seconds == 0.25
    assert profile.total_timeout_seconds == 0.25
    assert profile.max_retries == 2
    assert profile.retry_backoff_seconds == 1.0


def test_deadline_reached_during_privacy_review_prevents_repository_write() -> None:
    from unittest.mock import Mock

    from app.memory.consolidation.contracts import (
        ConsolidationPlanType,
        ConsolidationPrivacyReview,
    )
    from app.memory.consolidation.service import apply_validated_plan

    planned = (_detail(MEMORY_ONE), _detail(MEMORY_TWO))
    assessment = PrivacyAssessment(
        classification=SemanticClassification.NOT_SENSITIVE,
        subject_scope=SubjectScope.SELF,
        category=SemanticPrivacyCategory.NONE,
        reason_code=SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT,
        classifier_version="privacy-classifier-v7",
        model_id="privacy-model",
        model_digest="privacy-digest",
        prompt_version="privacy-prompt-v3",
        policy_version="policy-v9",
    )
    reviewed_candidate = ApprovedMemoryCandidate(
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object="紅茶",
        ),
        normalized_text="ユーザーは紅茶を好む",
    )
    monotonic_values = iter((0.75, 1.0))
    privacy_reviewer = Mock()
    privacy_reviewer.review.return_value = ConsolidationPrivacyReview(
        candidate=reviewed_candidate,
        assessment=assessment,
    )
    repository = Mock()

    outcome = apply_validated_plan(
        plan=_plan("MERGE"),
        planned=planned,
        current=planned,
        repository=repository,
        privacy_reviewer=privacy_reviewer,
        consolidated_at=datetime(2026, 8, 23, tzinfo=UTC),
        model_id="consolidation-model",
        prompt_version="consolidation-v1",
        policy_version="policy-v9",
        deadline=1.0,
        monotonic_clock=lambda: next(monotonic_values),
    )

    assert outcome.plan_type is ConsolidationPlanType.NOOP
    assert outcome.reason_code == "TIMEOUT"
    privacy_reviewer.review.assert_called_once_with(
        memory_type=MemoryType.USER_PREFERENCE,
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object="紅茶",
        ),
        timeout_seconds=0.25,
    )
    repository.apply_consolidation.assert_not_called()


def test_exhausted_deadline_prevents_privacy_review_from_starting() -> None:
    from unittest.mock import Mock

    from app.memory.consolidation.contracts import ConsolidationPlanType
    from app.memory.consolidation.service import apply_validated_plan

    planned = (_detail(MEMORY_ONE), _detail(MEMORY_TWO))
    repository = Mock()
    privacy_reviewer = Mock()

    outcome = apply_validated_plan(
        plan=_plan("SUPERSEDE"),
        planned=planned,
        current=planned,
        repository=repository,
        privacy_reviewer=privacy_reviewer,
        consolidated_at=datetime(2026, 8, 23, tzinfo=UTC),
        model_id="consolidation-model",
        prompt_version="consolidation-v1",
        policy_version="policy-v1",
        deadline=1.0,
        monotonic_clock=lambda: 1.0,
    )

    assert outcome.plan_type is ConsolidationPlanType.NOOP
    assert outcome.reason_code == "TIMEOUT"
    privacy_reviewer.review.assert_not_called()
    repository.apply_consolidation.assert_not_called()


@pytest.mark.parametrize("plan_type", ("KEEP", "DELETE_EXACT_DUPLICATE"))
def test_deadline_gate_prevents_every_non_content_repository_write(
    plan_type: str,
) -> None:
    from unittest.mock import Mock

    from app.memory.consolidation.contracts import ConsolidationPlanType
    from app.memory.consolidation.service import apply_validated_plan

    planned = (_detail(MEMORY_ONE), _detail(MEMORY_TWO))
    if plan_type == "DELETE_EXACT_DUPLICATE":
        planned = (
            planned[0],
            replace(
                planned[0],
                memory=replace(planned[0].memory, id=MEMORY_TWO),
            ),
        )
    repository = Mock()
    privacy_reviewer = Mock()

    outcome = apply_validated_plan(
        plan=_plan(plan_type),
        planned=planned,
        current=planned,
        repository=repository,
        privacy_reviewer=privacy_reviewer,
        consolidated_at=datetime(2026, 8, 23, tzinfo=UTC),
        model_id="consolidation-model",
        prompt_version="consolidation-v1",
        policy_version="policy-v1",
        deadline=1.0,
        monotonic_clock=lambda: 1.0,
    )

    assert outcome.plan_type is ConsolidationPlanType.NOOP
    assert outcome.reason_code == "TIMEOUT"
    repository.apply_consolidation.assert_not_called()
    privacy_reviewer.review.assert_not_called()

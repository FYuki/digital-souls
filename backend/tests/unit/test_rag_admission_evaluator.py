from __future__ import annotations

import functools
import importlib
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from app.conversation_history.models import TurnStatus
from app.memory.memory_policy import resolved_memory_policy
from app.privacy.contracts import (
    FindingReasonCode,
    PrivacyCategory,
    PrivacyFinding,
    ScanFailure,
    ScanFailureReasonCode,
    ScanSuccess,
    StorageScope,
)
from app.privacy.semantic.contracts import (
    PrivacyAssessment,
    SemanticAssessmentReasonCode,
    SemanticClassification,
    SemanticPrivacyCategory,
    SubjectScope,
)

if TYPE_CHECKING:
    from app.memory.admission.contracts import (
        ConversationSource,
        EpisodicEventType,
        EpisodicEventValue,
        EpisodicSubject,
        InteractionAspect,
        InteractionPreferenceValue,
        MemoryCandidate,
        MemoryType,
        PreferencePolarity,
        RagAdmissionDecision,
        UserPreferenceValue,
    )
    from app.memory.admission.evaluator import create_rag_admission_evaluator


def _with_admission_contract(test):
    @functools.wraps(test)
    def wrapped(*args, **kwargs):
        contracts = importlib.import_module("app.memory.admission.contracts")
        evaluator = importlib.import_module("app.memory.admission.evaluator")
        names = (
            "ConversationSource",
            "EpisodicEventType",
            "EpisodicEventValue",
            "EpisodicSubject",
            "InteractionAspect",
            "InteractionPreferenceValue",
            "MemoryCandidate",
            "MemoryType",
            "PreferencePolarity",
            "RagAdmissionDecision",
            "UserPreferenceValue",
        )
        globals().update({name: getattr(contracts, name) for name in names})
        globals()["create_rag_admission_evaluator"] = (
            evaluator.create_rag_admission_evaluator
        )
        return test(*args, **kwargs)

    return wrapped


def _source(
    status: TurnStatus = TurnStatus.COMPLETED,
    history_content_stored: bool = True,
) -> ConversationSource:
    return ConversationSource(status, history_content_stored)


def _preference_candidate(
    *,
    object_value: str = "コーヒー",
    source: ConversationSource | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        MemoryType.USER_PREFERENCE,
        UserPreferenceValue(PreferencePolarity.LIKE, object_value),
        _source() if source is None else source,
    )


def _assessment(
    classification: SemanticClassification = SemanticClassification.NOT_SENSITIVE,
    subject_scope: SubjectScope = SubjectScope.SELF,
    category: SemanticPrivacyCategory = SemanticPrivacyCategory.NONE,
    reason_code: SemanticAssessmentReasonCode = (
        SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT
    ),
) -> PrivacyAssessment:
    return PrivacyAssessment(
        classification=classification,
        subject_scope=subject_scope,
        category=category,
        reason_code=reason_code,
        classifier_version="test-classifier-v1",
        model_id="test-model",
        model_digest="sha256:test",
        prompt_version="test-prompt-v1",
        policy_version=resolved_memory_policy().policy_version,
    )


def _clean_scan() -> ScanSuccess:
    return ScanSuccess(())


def _failure() -> ScanFailure:
    policy_version = resolved_memory_policy().policy_version
    return ScanFailure(
        ScanFailureReasonCode.RECOGNIZER_ERROR,
        "test-recognizer-v1",
        policy_version,
    )


def _finding(
    category: PrivacyCategory,
    *,
    scope: StorageScope | None = None,
    start: int = 0,
) -> PrivacyFinding:
    policy_version = resolved_memory_policy().policy_version
    return PrivacyFinding(
        category=category,
        start=start,
        end=start + 1,
        confidence=1.0,
        reason_code=(
            FindingReasonCode.STORAGE_OPT_OUT_MATCH
            if category is PrivacyCategory.STORAGE_OPT_OUT
            else FindingReasonCode.DETERMINISTIC_MATCH
        ),
        recognizer_version="test-recognizer-v1",
        policy_version=policy_version,
        storage_scope=scope,
    )


def _evaluate(
    *,
    source_scan=ScanSuccess(()),
    slot_scans=None,
    assessment: PrivacyAssessment | None = None,
    candidate: MemoryCandidate | None = None,
):
    evaluator = create_rag_admission_evaluator(resolved_memory_policy().privacy)
    actual_candidate = candidate or _preference_candidate()
    actual_slot_scans = {"object": _clean_scan()} if slot_scans is None else slot_scans
    actual_assessment = _assessment() if assessment is None else assessment
    return evaluator.evaluate(
        source_scan=source_scan,
        candidate_slot_scans=actual_slot_scans,
        assessment=actual_assessment,
        candidate=actual_candidate,
    )


@pytest.mark.parametrize("scope", [StorageScope.RAG, StorageScope.BOTH])
@_with_admission_contract
def test_source_storage_opt_out_denies_user_request(scope: StorageScope) -> None:
    result = _evaluate(
        source_scan=ScanSuccess(
            (_finding(PrivacyCategory.STORAGE_OPT_OUT, scope=scope),)
        )
    )

    assert result.decision is RagAdmissionDecision.DENY_USER_REQUEST
    assert result.candidate is None


@_with_admission_contract
def test_source_sensitive_finding_denies_candidate() -> None:
    result = _evaluate(
        source_scan=ScanSuccess((_finding(PrivacyCategory.EMAIL),)),
    )

    assert result.decision is RagAdmissionDecision.DENY_SENSITIVE
    assert result.candidate is None


@pytest.mark.parametrize(
    ("source_scan", "slot_scans"),
    [
        (_failure(), {"object": ScanSuccess(())}),
        (ScanSuccess(()), {"object": _failure()}),
    ],
    ids=["source-scan", "slot-scan"],
)
@_with_admission_contract
def test_scan_failure_abstains_without_candidate(
    source_scan,
    slot_scans,
) -> None:
    result = _evaluate(source_scan=source_scan, slot_scans=slot_scans)

    assert result.decision is RagAdmissionDecision.ABSTAIN_UNKNOWN
    assert result.candidate is None


@_with_admission_contract
def test_general_semantic_content_is_not_memory_worthy() -> None:
    result = _evaluate(
        assessment=_assessment(subject_scope=SubjectScope.GENERAL),
    )

    assert result.decision is RagAdmissionDecision.NOT_MEMORY_WORTHY
    assert result.candidate is None


@pytest.mark.parametrize(
    ("candidate_kind", "slot_scans", "normalized_text"),
    [
        (
            "episodic",
            {"topic": ScanSuccess(())},
            "ユーザーが資格取得を達成した。",
        ),
        (
            "preference",
            {"object": ScanSuccess(())},
            "ユーザーはコーヒーを好む。",
        ),
        (
            "interaction",
            {"value": ScanSuccess(())},
            "ユーザーは日本語での会話を望む。",
        ),
    ],
)
@_with_admission_contract
def test_safe_allowlist_candidates_are_approved_without_confirmation(
    candidate_kind: str,
    slot_scans: dict[str, ScanSuccess],
    normalized_text: str,
) -> None:
    candidates = {
        "episodic": MemoryCandidate(
            MemoryType.EPISODIC_EVENT,
            EpisodicEventValue(
                EpisodicEventType.ACHIEVEMENT,
                EpisodicSubject.USER,
                "資格取得",
            ),
            _source(),
        ),
        "preference": _preference_candidate(),
        "interaction": MemoryCandidate(
            MemoryType.INTERACTION_PREFERENCE,
            InteractionPreferenceValue(InteractionAspect.LANGUAGE, "日本語"),
            _source(),
        ),
    }
    candidate = candidates[candidate_kind]
    result = _evaluate(candidate=candidate, slot_scans=slot_scans)

    assert result.decision is RagAdmissionDecision.ALLOW_STRUCTURED
    assert result.candidate is not None
    assert result.candidate.structured_value == candidate.structured_value
    assert result.candidate.normalized_text == normalized_text


@_with_admission_contract
def test_candidate_outside_allowlist_is_not_memory_worthy() -> None:
    candidate = object.__new__(MemoryCandidate)
    object.__setattr__(candidate, "memory_type", object())
    object.__setattr__(candidate, "structured_value", object())
    object.__setattr__(candidate, "source", _source())

    result = _evaluate(candidate=candidate, slot_scans={})

    assert result.decision is RagAdmissionDecision.NOT_MEMORY_WORTHY
    assert result.candidate is None


@pytest.mark.parametrize(
    "source_kind",
    [
        "source-missing",
        "history-content-not-stored",
        "privacy-skipped",
    ],
)
@_with_admission_contract
def test_unusable_source_turn_abstains(source_kind: str) -> None:
    sources = {
        "source-missing": None,
        "history-content-not-stored": _source(history_content_stored=False),
        "privacy-skipped": _source(TurnStatus.PRIVACY_SKIPPED, True),
    }
    candidate = MemoryCandidate(
        MemoryType.USER_PREFERENCE,
        UserPreferenceValue(PreferencePolarity.LIKE, "コーヒー"),
        sources[source_kind],
    )
    result = _evaluate(candidate=candidate)

    assert result.decision is RagAdmissionDecision.ABSTAIN_UNKNOWN
    assert result.candidate is None


@pytest.mark.parametrize(
    "reason_code",
    [
        SemanticAssessmentReasonCode.TIMEOUT,
        SemanticAssessmentReasonCode.MODEL_NOT_LOADED,
        SemanticAssessmentReasonCode.MODEL_UNAVAILABLE,
        SemanticAssessmentReasonCode.INVALID_OUTPUT,
        SemanticAssessmentReasonCode.UNKNOWN_CATEGORY,
        SemanticAssessmentReasonCode.UNKNOWN_LANGUAGE,
    ],
)
@_with_admission_contract
def test_classifier_abstention_reasons_fail_closed(
    reason_code: SemanticAssessmentReasonCode,
) -> None:
    assessment = _assessment(
        classification=SemanticClassification.ABSTAIN,
        subject_scope=SubjectScope.UNKNOWN,
        category=SemanticPrivacyCategory.UNKNOWN,
        reason_code=reason_code,
    )

    result = _evaluate(assessment=assessment)

    assert result.decision is RagAdmissionDecision.ABSTAIN_UNKNOWN
    assert result.candidate is None


@_with_admission_contract
def test_missing_assessment_abstains() -> None:
    evaluator = create_rag_admission_evaluator(resolved_memory_policy().privacy)

    result = evaluator.evaluate(
        source_scan=_clean_scan(),
        candidate_slot_scans={"object": _clean_scan()},
        assessment=None,
        candidate=_preference_candidate(),
    )

    assert result.decision is RagAdmissionDecision.ABSTAIN_UNKNOWN
    assert result.candidate is None


@_with_admission_contract
def test_semantic_sensitive_content_is_denied() -> None:
    assessment = _assessment(
        classification=SemanticClassification.SENSITIVE,
        category=SemanticPrivacyCategory.HEALTH,
        reason_code=SemanticAssessmentReasonCode.SENSITIVE_CONTENT,
    )

    result = _evaluate(assessment=assessment)

    assert result.decision is RagAdmissionDecision.DENY_SENSITIVE
    assert result.candidate is None


@_with_admission_contract
def test_not_sensitive_assessment_does_not_override_deterministic_finding() -> None:
    result = _evaluate(
        source_scan=ScanSuccess((_finding(PrivacyCategory.EMAIL),)),
        assessment=_assessment(),
    )

    assert result.decision is RagAdmissionDecision.DENY_SENSITIVE
    assert result.candidate is None


@_with_admission_contract
def test_policy_placeholder_in_slot_is_denied_without_a_scan_finding() -> None:
    result = _evaluate(candidate=_preference_candidate(object_value="連絡先[EMAIL]"))

    assert result.decision is RagAdmissionDecision.DENY_SENSITIVE
    assert result.candidate is None


@_with_admission_contract
def test_each_slot_scan_is_evaluated_independently() -> None:
    candidate = MemoryCandidate(
        MemoryType.USER_PREFERENCE,
        UserPreferenceValue(
            PreferencePolarity.PREFER_OVER,
            "コーヒー",
            alternative="紅茶",
        ),
        _source(),
    )

    result = _evaluate(
        candidate=candidate,
        slot_scans={
            "object": _clean_scan(),
            "alternative": ScanSuccess((_finding(PrivacyCategory.EMAIL),)),
        },
    )

    assert result.decision is RagAdmissionDecision.DENY_SENSITIVE
    assert result.candidate is None


@pytest.mark.parametrize(
    "slot_scans",
    [{}, {"object": ScanSuccess(()), "unexpected": ScanSuccess(())}],
    ids=["missing", "extra"],
)
@_with_admission_contract
def test_slot_scan_keys_must_exactly_match_candidate_slots(slot_scans) -> None:
    result = _evaluate(slot_scans=slot_scans)

    assert result.decision is RagAdmissionDecision.ABSTAIN_UNKNOWN


@_with_admission_contract
def test_manual_correction_can_be_allowed_without_conversation_source() -> None:
    evaluator = create_rag_admission_evaluator(resolved_memory_policy().privacy)
    candidate = _preference_candidate()
    candidate = replace(candidate, source=None)

    result = evaluator.evaluate_manual_correction(
        candidate_slot_scans={"object": _clean_scan()},
        assessment=_assessment(),
        candidate=candidate,
    )

    assert result.decision is RagAdmissionDecision.ALLOW_STRUCTURED
    assert result.candidate is not None


@pytest.mark.parametrize(
    "assessment",
    [
        _assessment(
            classification=SemanticClassification.SENSITIVE,
            category=SemanticPrivacyCategory.HEALTH,
            reason_code=SemanticAssessmentReasonCode.SENSITIVE_CONTENT,
        ),
        None,
    ],
    ids=["sensitive", "missing-assessment"],
)
@_with_admission_contract
def test_manual_correction_rejects_every_non_allow_decision(assessment) -> None:
    evaluator = create_rag_admission_evaluator(resolved_memory_policy().privacy)
    candidate = replace(_preference_candidate(), source=None)

    result = evaluator.evaluate_manual_correction(
        candidate_slot_scans={"object": _clean_scan()},
        assessment=assessment,
        candidate=candidate,
    )

    assert result.decision is not RagAdmissionDecision.ALLOW_STRUCTURED
    assert result.candidate is None


@_with_admission_contract
def test_source_opt_out_wins_when_source_also_has_sensitive_finding() -> None:
    result = _evaluate(
        source_scan=ScanSuccess(
            (
                _finding(PrivacyCategory.STORAGE_OPT_OUT, scope=StorageScope.RAG),
                _finding(PrivacyCategory.EMAIL, start=1),
            )
        )
    )

    assert result.decision is RagAdmissionDecision.DENY_USER_REQUEST
    assert result.candidate is None


@_with_admission_contract
def test_source_sensitive_finding_wins_over_slot_scan_failure() -> None:
    result = _evaluate(
        source_scan=ScanSuccess((_finding(PrivacyCategory.EMAIL),)),
        slot_scans={"object": _failure()},
    )

    assert result.decision is RagAdmissionDecision.DENY_SENSITIVE
    assert result.candidate is None


@pytest.mark.parametrize(
    ("source_scan", "candidate_slot_scans"),
    [
        (
            ScanSuccess((_finding(PrivacyCategory.EMAIL),)),
            {"object": _clean_scan()},
        ),
        (
            ScanSuccess(
                (
                    _finding(
                        PrivacyCategory.STORAGE_OPT_OUT,
                        scope=StorageScope.RAG,
                    ),
                )
            ),
            {"object": _clean_scan()},
        ),
        (
            _clean_scan(),
            {"object": ScanSuccess((_finding(PrivacyCategory.EMAIL),))},
        ),
    ],
    ids=["source-sensitive", "storage-opt-out", "slot-sensitive"],
)
@_with_admission_contract
def test_deterministic_rejection_does_not_require_classifier(
    source_scan,
    candidate_slot_scans,
) -> None:
    evaluator = create_rag_admission_evaluator(resolved_memory_policy().privacy)
    classifier_calls = 0

    if evaluator.requires_semantic_assessment(
        source_scan=source_scan,
        candidate_slot_scans=candidate_slot_scans,
        candidate=_preference_candidate(),
    ):
        classifier_calls += 1

    assert classifier_calls == 0


@_with_admission_contract
def test_clean_candidate_requires_semantic_assessment() -> None:
    evaluator = create_rag_admission_evaluator(resolved_memory_policy().privacy)

    required = evaluator.requires_semantic_assessment(
        source_scan=_clean_scan(),
        candidate_slot_scans={"object": _clean_scan()},
        candidate=_preference_candidate(),
    )

    assert required is True


@_with_admission_contract
def test_evaluation_is_deterministic_for_identical_input() -> None:
    evaluator = create_rag_admission_evaluator(resolved_memory_policy().privacy)
    arguments = {
        "source_scan": _clean_scan(),
        "candidate_slot_scans": {"object": _clean_scan()},
        "assessment": _assessment(),
        "candidate": _preference_candidate(),
    }

    first = evaluator.evaluate(**arguments)
    second = evaluator.evaluate(**arguments)

    assert first == second


@_with_admission_contract
def test_denied_input_is_not_emitted_to_logs_or_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "PRIVATE-SLOT-VALUE-71A2"

    with caplog.at_level(logging.DEBUG):
        result = _evaluate(
            candidate=_preference_candidate(object_value=sentinel),
            slot_scans={"object": ScanSuccess((_finding(PrivacyCategory.EMAIL),))},
        )

    observable = caplog.text + repr(result)
    assert sentinel not in observable
    assert "start=0" not in observable
    assert "end=1" not in observable

from dataclasses import FrozenInstanceError, fields
from enum import Enum
import inspect

import pytest


EXPECTED_CATEGORIES = {
    "HEALTH",
    "MENTAL_STATE",
    "SELF_HARM",
    "ABUSE_OR_SEXUAL_VIOLENCE",
    "FINANCIAL_SITUATION",
    "THIRD_PARTY_PRIVATE",
    "OTHER_SENSITIVE",
    "NONE",
    "UNKNOWN",
}


def test_classifier_protocol_exposes_text_and_call_profile_contract() -> None:
    from app.privacy.semantic.classifier import SemanticPrivacyClassifier

    parameters = inspect.signature(SemanticPrivacyClassifier.classify).parameters

    assert list(parameters) == ["self", "text", "profile"]
    assert parameters["text"].annotation in {str, "str"}


def _assessment(**overrides):
    from app.privacy.semantic.contracts import (
        PrivacyAssessment,
        SemanticAssessmentReasonCode,
        SemanticClassification,
        SemanticPrivacyCategory,
        SubjectScope,
    )

    values = {
        "classification": SemanticClassification.SENSITIVE,
        "subject_scope": SubjectScope.SELF,
        "category": SemanticPrivacyCategory.HEALTH,
        "reason_code": SemanticAssessmentReasonCode.SENSITIVE_CONTENT,
        "classifier_version": "semantic-privacy-classifier-v1",
        "model_id": "gemma4:e4b",
        "model_digest": "sha256:" + "a" * 64,
        "prompt_version": "semantic-privacy-prompt-v1",
        "policy_version": "privacy-policy-v1",
    }
    values.update(overrides)
    return PrivacyAssessment(**values)


def test_semantic_contracts_use_closed_enums() -> None:
    from app.privacy.semantic.contracts import (
        SemanticAssessmentReasonCode,
        SemanticClassification,
        SemanticPrivacyCategory,
        SubjectScope,
    )

    assert {item.value for item in SemanticPrivacyCategory} == EXPECTED_CATEGORIES
    assert {item.value for item in SemanticClassification} == {
        "SENSITIVE",
        "NOT_SENSITIVE",
        "ABSTAIN",
    }
    assert {item.value for item in SubjectScope} == {
        "SELF",
        "THIRD_PARTY",
        "GENERAL",
        "UNKNOWN",
    }
    assert issubclass(SemanticAssessmentReasonCode, Enum)
    assert {
        "SENSITIVE_CONTENT",
        "NO_SENSITIVE_CONTENT",
        "TIMEOUT",
        "MODEL_NOT_LOADED",
        "MODEL_UNAVAILABLE",
        "INVALID_OUTPUT",
        "UNKNOWN_CATEGORY",
        "UNKNOWN_LANGUAGE",
    }.issubset({item.value for item in SemanticAssessmentReasonCode})


def test_assessment_is_immutable_and_contains_only_semantic_provenance() -> None:
    assessment = _assessment()

    assert {field.name for field in fields(assessment)} == {
        "classification",
        "subject_scope",
        "category",
        "reason_code",
        "classifier_version",
        "model_id",
        "model_digest",
        "prompt_version",
        "policy_version",
    }
    with pytest.raises(FrozenInstanceError):
        assessment.classification = assessment.classification


def test_assessment_is_independent_from_deterministic_privacy_finding() -> None:
    from app.privacy.contracts import PrivacyFinding

    assessment = _assessment()

    assert not isinstance(assessment, PrivacyFinding)
    assert not any(
        field.name
        in {
            "storage_scope",
            "storage_allowed",
            "character_id",
            "provider_id",
            "allowlist",
            "rag_admission",
        }
        for field in fields(assessment)
    )


@pytest.mark.parametrize(
    ("classification", "scope", "category"),
    [
        ("SENSITIVE", "SELF", "NONE"),
        ("NOT_SENSITIVE", "GENERAL", "HEALTH"),
        ("ABSTAIN", "SELF", "UNKNOWN"),
        ("ABSTAIN", "UNKNOWN", "HEALTH"),
    ],
)
def test_assessment_rejects_inconsistent_classification_combinations(
    classification: str,
    scope: str,
    category: str,
) -> None:
    from app.privacy.semantic.contracts import (
        SemanticClassification,
        SemanticPrivacyCategory,
        SubjectScope,
    )

    with pytest.raises(ValueError):
        _assessment(
            classification=SemanticClassification(classification),
            subject_scope=SubjectScope(scope),
            category=SemanticPrivacyCategory(category),
        )


@pytest.mark.parametrize(
    ("classification", "scope", "category", "reason_code"),
    [
        ("SENSITIVE", "SELF", "HEALTH", "NO_SENSITIVE_CONTENT"),
        ("NOT_SENSITIVE", "GENERAL", "NONE", "SENSITIVE_CONTENT"),
        ("ABSTAIN", "UNKNOWN", "UNKNOWN", "SENSITIVE_CONTENT"),
    ],
)
def test_assessment_rejects_reason_codes_from_a_different_classification(
    classification: str,
    scope: str,
    category: str,
    reason_code: str,
) -> None:
    from app.privacy.semantic.contracts import (
        SemanticAssessmentReasonCode,
        SemanticClassification,
        SemanticPrivacyCategory,
        SubjectScope,
    )

    with pytest.raises(ValueError):
        _assessment(
            classification=SemanticClassification(classification),
            subject_scope=SubjectScope(scope),
            category=SemanticPrivacyCategory(category),
            reason_code=SemanticAssessmentReasonCode(reason_code),
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("classifier_version", ""),
        ("model_id", " "),
        ("model_digest", ""),
        ("prompt_version", " "),
        ("policy_version", ""),
    ],
)
def test_assessment_rejects_blank_provenance(field_name: str, value: str) -> None:
    with pytest.raises(ValueError):
        _assessment(**{field_name: value})


def test_call_profiles_are_immutable_and_define_bounded_attempts() -> None:
    from app.privacy.semantic.contracts import ADMISSION, QUERY_GATE

    assert (
        QUERY_GATE.name,
        QUERY_GATE.timeout_seconds,
        QUERY_GATE.max_retries,
        QUERY_GATE.retry_backoff_seconds,
        QUERY_GATE.total_timeout_seconds,
    ) == ("QUERY_GATE", 2.0, 0, 0.0, 2.0)
    assert (
        ADMISSION.name,
        ADMISSION.timeout_seconds,
        ADMISSION.max_retries,
        ADMISSION.retry_backoff_seconds,
        ADMISSION.total_timeout_seconds,
    ) == ("ADMISSION", 15.0, 2, 1.0, 35.0)
    with pytest.raises(FrozenInstanceError):
        QUERY_GATE.max_retries = 1

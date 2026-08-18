from __future__ import annotations

import inspect
import math
from dataclasses import FrozenInstanceError, fields
from enum import Enum

import pytest

from tests.privacy_test_support import POLICY_VERSION


EXPECTED_CATEGORIES = {
    "API_KEY",
    "ACCESS_TOKEN",
    "SESSION_COOKIE",
    "RECOVERY_CODE",
    "PASSWORD",
    "PIN",
    "PRIVATE_KEY",
    "CRYPTO_PRIVATE_KEY",
    "SEED_PHRASE",
    "PAYMENT_CARD",
    "CVV",
    "BANK_ACCOUNT",
    "BANK_CREDENTIAL",
    "EMAIL",
    "PHONE",
    "PRIVATE_CONTACT",
    "GOVERNMENT_ID",
    "PRECISE_ADDRESS",
    "PRECISE_LOCATION",
    "STORAGE_OPT_OUT",
    "POLICY_ADDED_SENSITIVE",
}
DETECTED_VALUE = "synthetic-secret-value"


def _finding(**overrides):
    from app.privacy.contracts import (
        FindingReasonCode,
        PrivacyCategory,
        PrivacyFinding,
    )

    values = {
        "category": PrivacyCategory.API_KEY,
        "start": 4,
        "end": 10,
        "confidence": 1.0,
        "reason_code": FindingReasonCode.DETERMINISTIC_MATCH,
        "recognizer_version": "credentials-v1",
        "policy_version": POLICY_VERSION,
        "storage_scope": None,
    }
    values.update(overrides)
    return PrivacyFinding(**values)


def test_should_expose_scanner_protocol_with_text_only_scan_contract() -> None:
    from app.privacy.contracts import PrivacyScanner

    parameters = inspect.signature(PrivacyScanner.scan).parameters

    assert list(parameters) == ["self", "text"]
    assert parameters["text"].annotation in {str, "str"}


def test_should_expose_only_domain_contracts_from_privacy_package() -> None:
    import app.privacy as privacy

    assert set(privacy.__all__) == {
        "ConversationHistoryAction",
        "ConversationHistoryDecision",
        "FindingReasonCode",
        "HistoryDecisionReasonCode",
        "PrivacyCategory",
        "PrivacyFinding",
        "PrivacyScanner",
        "ScanFailure",
        "ScanFailureReasonCode",
        "ScanResult",
        "ScanSuccess",
        "StorageScope",
    }


def test_should_define_exactly_the_fixed_privacy_categories() -> None:
    from app.privacy.contracts import PrivacyCategory

    assert {category.value for category in PrivacyCategory} == EXPECTED_CATEGORIES


def test_should_define_only_fixed_storage_scopes() -> None:
    from app.privacy.contracts import StorageScope

    assert {scope.value for scope in StorageScope} == {"RAG", "BOTH"}


def test_should_represent_reason_codes_as_closed_enums() -> None:
    from app.privacy.contracts import (
        FindingReasonCode,
        HistoryDecisionReasonCode,
        ScanFailureReasonCode,
    )

    assert issubclass(FindingReasonCode, Enum)
    assert issubclass(ScanFailureReasonCode, Enum)
    assert issubclass(HistoryDecisionReasonCode, Enum)
    assert {reason.value for reason in ScanFailureReasonCode} == {
        "INVALID_INPUT",
        "RECOGNIZER_ERROR",
        "INVALID_RECOGNIZER_RESULT",
    }
    assert all(reason.value.strip() == reason.value for reason in FindingReasonCode)
    assert all(
        reason.value.strip() == reason.value for reason in HistoryDecisionReasonCode
    )


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_should_accept_finite_confidence_at_inclusive_boundaries(
    confidence: float,
) -> None:
    finding = _finding(confidence=confidence)

    assert finding.confidence == confidence


@pytest.mark.parametrize(
    "confidence",
    [-0.01, 1.01, math.nan, math.inf, -math.inf],
)
def test_should_reject_invalid_or_non_finite_confidence(confidence: float) -> None:
    with pytest.raises(ValueError):
        _finding(confidence=confidence)


def test_should_reject_non_enum_finding_reason_code() -> None:
    with pytest.raises(TypeError, match="FindingReasonCode"):
        _finding(reason_code="DETERMINISTIC_MATCH")


def test_should_allow_storage_scope_only_for_storage_opt_out() -> None:
    from app.privacy.contracts import PrivacyCategory, StorageScope

    finding = _finding(
        category=PrivacyCategory.STORAGE_OPT_OUT,
        storage_scope=StorageScope.RAG,
    )

    assert finding.storage_scope is StorageScope.RAG


def test_should_reject_storage_scope_for_non_opt_out_category() -> None:
    from app.privacy.contracts import StorageScope

    with pytest.raises(ValueError):
        _finding(storage_scope=StorageScope.BOTH)


def test_should_reject_missing_storage_scope_for_opt_out_category() -> None:
    from app.privacy.contracts import PrivacyCategory

    with pytest.raises(ValueError):
        _finding(category=PrivacyCategory.STORAGE_OPT_OUT)


def test_should_not_store_detected_value_or_source_mapping_in_finding() -> None:
    finding = _finding()

    assert {field.name for field in fields(finding)} == {
        "category",
        "start",
        "end",
        "confidence",
        "reason_code",
        "recognizer_version",
        "policy_version",
        "storage_scope",
    }
    assert DETECTED_VALUE not in repr(finding)
    assert "start" not in repr(finding)
    assert "end" not in repr(finding)


def test_should_keep_finding_immutable() -> None:
    finding = _finding()

    with pytest.raises(FrozenInstanceError):
        finding.confidence = 0.5


def test_should_hold_scan_success_findings_as_ordered_tuple() -> None:
    from app.privacy.contracts import PrivacyCategory, ScanSuccess

    category_earlier = _finding(start=1, end=3, category=PrivacyCategory.EMAIL)
    category_later = _finding(start=1, end=3, category=PrivacyCategory.PHONE)
    later = _finding(start=9, end=11, category=PrivacyCategory.EMAIL)

    result = ScanSuccess((category_earlier, category_later, later))

    assert result.findings == (category_earlier, category_later, later)
    assert isinstance(result.findings, tuple)


def test_should_reject_unsorted_scan_success_findings() -> None:
    from app.privacy.contracts import PrivacyCategory, ScanSuccess

    later = _finding(start=6, end=11, category=PrivacyCategory.PHONE)
    earlier = _finding(start=0, end=5, category=PrivacyCategory.EMAIL)

    with pytest.raises(ValueError, match="deterministic order"):
        ScanSuccess((later, earlier))


def test_should_reject_non_finding_scan_success_element() -> None:
    from app.privacy.contracts import ScanSuccess

    with pytest.raises(TypeError, match="PrivacyFinding"):
        ScanSuccess(("not-a-finding",))


def test_should_keep_scan_failure_metadata_only_and_safe_to_repr() -> None:
    from app.privacy.contracts import ScanFailure, ScanFailureReasonCode

    failure = ScanFailure(
        reason_code=ScanFailureReasonCode.RECOGNIZER_ERROR,
        recognizer_version="credentials-v1",
        policy_version=POLICY_VERSION,
    )

    assert {field.name for field in fields(failure)} == {
        "reason_code",
        "recognizer_version",
        "policy_version",
    }
    assert DETECTED_VALUE not in repr(failure)
    assert "span" not in repr(failure).lower()


@pytest.mark.parametrize("field_name", ["recognizer_version", "policy_version"])
def test_should_reject_blank_scan_failure_versions(field_name: str) -> None:
    from app.privacy.contracts import ScanFailure, ScanFailureReasonCode

    values = {
        "reason_code": ScanFailureReasonCode.RECOGNIZER_ERROR,
        "recognizer_version": "credentials-v1",
        "policy_version": POLICY_VERSION,
    }
    values[field_name] = " "

    with pytest.raises(ValueError):
        ScanFailure(**values)


def test_should_reject_non_enum_scan_failure_reason_code() -> None:
    from app.privacy.contracts import ScanFailure

    with pytest.raises(TypeError, match="ScanFailureReasonCode"):
        ScanFailure(
            reason_code="RECOGNIZER_ERROR",
            recognizer_version="credentials-v1",
            policy_version=POLICY_VERSION,
        )


def test_should_store_content_only_for_store_masked_decision() -> None:
    from app.privacy.contracts import (
        ConversationHistoryAction,
        ConversationHistoryDecision,
        HistoryDecisionReasonCode,
    )

    decision = ConversationHistoryDecision(
        action=ConversationHistoryAction.STORE_MASKED,
        reason_code=HistoryDecisionReasonCode.MASKED,
        sanitizer_version="history-sanitizer-v1",
        policy_version=POLICY_VERSION,
        content="safe [EMAIL]",
    )

    assert decision.content == "safe [EMAIL]"


def test_should_not_expose_store_masked_content_in_decision_repr() -> None:
    from app.privacy.contracts import (
        ConversationHistoryAction,
        ConversationHistoryDecision,
        HistoryDecisionReasonCode,
    )

    decision = ConversationHistoryDecision(
        action=ConversationHistoryAction.STORE_MASKED,
        reason_code=HistoryDecisionReasonCode.MASKED,
        sanitizer_version="history-sanitizer-v1",
        policy_version=POLICY_VERSION,
        content=DETECTED_VALUE,
    )

    assert DETECTED_VALUE not in repr(decision)
    assert "content" not in repr(decision)


def test_should_reject_content_for_skip_content_decision() -> None:
    from app.privacy.contracts import (
        ConversationHistoryAction,
        ConversationHistoryDecision,
        HistoryDecisionReasonCode,
    )

    with pytest.raises(ValueError):
        ConversationHistoryDecision(
            action=ConversationHistoryAction.SKIP_CONTENT,
            reason_code=HistoryDecisionReasonCode.SCAN_FAILURE,
            sanitizer_version="history-sanitizer-v1",
            policy_version=POLICY_VERSION,
            content=DETECTED_VALUE,
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "expected_type"),
    [
        ("action", "STORE_MASKED", "ConversationHistoryAction"),
        ("reason_code", "MASKED", "HistoryDecisionReasonCode"),
    ],
)
def test_should_reject_non_enum_history_decision_fields(
    field_name: str,
    invalid_value: str,
    expected_type: str,
) -> None:
    from app.privacy.contracts import (
        ConversationHistoryAction,
        ConversationHistoryDecision,
        HistoryDecisionReasonCode,
    )

    values = {
        "action": ConversationHistoryAction.STORE_MASKED,
        "reason_code": HistoryDecisionReasonCode.MASKED,
        "sanitizer_version": "history-sanitizer-v1",
        "policy_version": POLICY_VERSION,
        "content": "safe [EMAIL]",
    }
    values[field_name] = invalid_value

    with pytest.raises(TypeError, match=expected_type):
        ConversationHistoryDecision(**values)


def test_should_keep_skip_decision_repr_free_of_content_and_positions() -> None:
    from app.privacy.contracts import (
        ConversationHistoryAction,
        ConversationHistoryDecision,
        HistoryDecisionReasonCode,
    )

    decision = ConversationHistoryDecision(
        action=ConversationHistoryAction.SKIP_CONTENT,
        reason_code=HistoryDecisionReasonCode.SCAN_FAILURE,
        sanitizer_version="history-sanitizer-v1",
        policy_version=POLICY_VERSION,
        content=None,
    )

    rendered = repr(decision)
    assert DETECTED_VALUE not in rendered
    assert "start" not in rendered
    assert "end" not in rendered

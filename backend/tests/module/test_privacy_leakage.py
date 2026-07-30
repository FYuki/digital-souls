from __future__ import annotations

import logging

from tests.privacy_test_support import POLICY_VERSION


SENSITIVE_TEXT = "synthetic-sensitive-value"


def _all_log_text(records: list[logging.LogRecord]) -> str:
    return "\n".join(record.getMessage() for record in records)


def test_should_not_leak_raw_text_or_recognizer_exception_to_logs_or_result(
    caplog,
) -> None:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.contracts import ScanFailure
    from app.privacy.scanner import DeterministicPrivacyScanner

    class FailingRecognizer:
        version = "failing-v1"

        def recognize(self, text: str):
            raise RuntimeError(f"recognizer saw {text} at span 0:25")

    scanner = DeterministicPrivacyScanner(
        resolved_memory_policy().privacy,
        recognizers=(FailingRecognizer(),),
    )

    with caplog.at_level(logging.DEBUG):
        result = scanner.scan(SENSITIVE_TEXT)

    assert isinstance(result, ScanFailure)
    observed = repr(result) + _all_log_text(caplog.records)
    for forbidden in (
        SENSITIVE_TEXT,
        "0:25",
        "source_map",
        "source map",
        "hash",
    ):
        assert forbidden not in observed


def test_should_not_leak_raw_text_when_sanitizer_rejects_invalid_span(
    caplog,
) -> None:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.contracts import (
        FindingReasonCode,
        PrivacyCategory,
        PrivacyFinding,
        ScanSuccess,
    )
    from app.privacy.history_sanitizer import create_history_sanitizer
    from tests.privacy_test_support import StubScanner

    invalid = object.__new__(PrivacyFinding)
    values = {
        "category": PrivacyCategory.PASSWORD,
        "start": -1,
        "end": len(SENSITIVE_TEXT),
        "confidence": 1.0,
        "reason_code": FindingReasonCode.DETERMINISTIC_MATCH,
        "recognizer_version": "credentials-v1",
        "policy_version": POLICY_VERSION,
        "storage_scope": None,
    }
    for name, value in values.items():
        object.__setattr__(invalid, name, value)
    scan_result = object.__new__(ScanSuccess)
    object.__setattr__(scan_result, "findings", (invalid,))
    policy = resolved_memory_policy().privacy
    sanitizer = create_history_sanitizer(
        StubScanner(scan_result, []),
        policy,
    )

    with caplog.at_level(logging.DEBUG):
        decision = sanitizer.sanitize_current_user(SENSITIVE_TEXT)

    observed = repr(decision) + _all_log_text(caplog.records)
    assert decision.content is None
    assert SENSITIVE_TEXT not in observed
    assert "-1" not in observed
    assert "source_map" not in observed


def test_should_send_only_masked_content_to_persistence_boundary() -> None:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.history_sanitizer import create_history_sanitizer
    from app.privacy.scanner import create_privacy_scanner

    policy = resolved_memory_policy().privacy
    sanitizer = create_history_sanitizer(create_privacy_scanner(policy), policy)
    decision = sanitizer.sanitize_current_user(
        f"password: {SENSITIVE_TEXT}"
    )
    persisted: list[str] = []
    if decision.content is not None:
        persisted.append(decision.content)

    assert persisted == ["password: [PASSWORD]"]
    assert all(SENSITIVE_TEXT not in record for record in persisted)


def test_should_send_no_content_to_persistence_boundary_for_skip() -> None:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.history_sanitizer import create_history_sanitizer
    from app.privacy.scanner import create_privacy_scanner

    policy = resolved_memory_policy().privacy
    sanitizer = create_history_sanitizer(create_privacy_scanner(policy), policy)
    decision = sanitizer.sanitize_current_user(
        f"{SENSITIVE_TEXT} は履歴に残さないで"
    )
    persisted: list[str] = []
    if decision.content is not None:
        persisted.append(decision.content)

    assert decision.content is None
    assert persisted == []

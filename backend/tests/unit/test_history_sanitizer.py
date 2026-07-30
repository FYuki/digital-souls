from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.privacy_test_support import POLICY_VERSION, StubScanner


PLACEHOLDERS = {
    "API_KEY": "[API_KEY]",
    "ACCESS_TOKEN": "[ACCESS_TOKEN]",
    "SESSION_COOKIE": "[ACCESS_TOKEN]",
    "RECOVERY_CODE": "[SENSITIVE]",
    "PASSWORD": "[PASSWORD]",
    "PIN": "[PASSWORD]",
    "PRIVATE_KEY": "[PRIVATE_KEY]",
    "CRYPTO_PRIVATE_KEY": "[PRIVATE_KEY]",
    "SEED_PHRASE": "[PRIVATE_KEY]",
    "PAYMENT_CARD": "[PAYMENT_CARD]",
    "CVV": "[PAYMENT_CARD]",
    "BANK_ACCOUNT": "[BANK_ACCOUNT]",
    "BANK_CREDENTIAL": "[BANK_ACCOUNT]",
    "EMAIL": "[EMAIL]",
    "PHONE": "[PHONE]",
    "PRIVATE_CONTACT": "[PHONE]",
    "GOVERNMENT_ID": "[GOVERNMENT_ID]",
    "PRECISE_ADDRESS": "[ADDRESS]",
    "PRECISE_LOCATION": "[LOCATION]",
    "POLICY_ADDED_SENSITIVE": "[SENSITIVE]",
}


def _finding(
    *,
    category: str = "EMAIL",
    start: object = 0,
    end: object = 5,
    scope: str | None = None,
):
    from app.privacy.contracts import (
        FindingReasonCode,
        PrivacyCategory,
        PrivacyFinding,
        StorageScope,
    )

    finding = object.__new__(PrivacyFinding)
    values = {
        "category": PrivacyCategory(category),
        "start": start,
        "end": end,
        "confidence": 1.0,
        "reason_code": FindingReasonCode.DETERMINISTIC_MATCH,
        "recognizer_version": "test-recognizer-v1",
        "policy_version": POLICY_VERSION,
        "storage_scope": StorageScope(scope) if scope else None,
    }
    for name, value in values.items():
        object.__setattr__(finding, name, value)
    return finding


def _success(*findings):
    from app.privacy.contracts import ScanSuccess

    result = object.__new__(ScanSuccess)
    object.__setattr__(result, "findings", tuple(findings))
    return result


def _sanitizer(scan_result):
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.history_sanitizer import create_history_sanitizer

    scanner = StubScanner(result=scan_result, calls=[])
    sanitizer = create_history_sanitizer(scanner, resolved_memory_policy().privacy)
    return sanitizer, scanner


def _assert_store_masked(decision, content: str) -> None:
    from app.privacy.contracts import ConversationHistoryAction

    assert decision.action is ConversationHistoryAction.STORE_MASKED
    assert decision.content == content
    assert decision.policy_version == POLICY_VERSION
    assert decision.sanitizer_version


def _assert_skip_content(decision) -> None:
    from app.privacy.contracts import ConversationHistoryAction

    assert decision.action is ConversationHistoryAction.SKIP_CONTENT
    assert decision.content is None
    assert decision.policy_version == POLICY_VERSION
    assert decision.sanitizer_version


def test_should_store_unchanged_text_when_scanner_finds_nothing() -> None:
    text = "今日は少し不安だけれど散歩できた"
    sanitizer, scanner = _sanitizer(_success())

    decision = sanitizer.sanitize_current_user(text)

    _assert_store_masked(decision, text)
    assert scanner.calls == [text]


@pytest.mark.parametrize(
    ("category", "placeholder"),
    PLACEHOLDERS.items(),
)
def test_should_replace_each_maskable_category_with_fixed_placeholder(
    category: str,
    placeholder: str,
) -> None:
    sanitizer, _scanner = _sanitizer(
        _success(_finding(category=category, start=7, end=12))
    )

    decision = sanitizer.sanitize_current_user("before VALUE after")

    _assert_store_masked(decision, f"before {placeholder} after")


def test_should_replace_non_overlapping_spans_from_the_original_coordinates() -> None:
    sanitizer, _scanner = _sanitizer(
        _success(
            _finding(category="EMAIL", start=0, end=5),
            _finding(category="PHONE", start=6, end=11),
        )
    )

    decision = sanitizer.sanitize_current_user("EMAIL PHONE")

    _assert_store_masked(decision, "[EMAIL] [PHONE]")


def test_should_skip_content_for_unsorted_forged_scan_success() -> None:
    sanitizer, _scanner = _sanitizer(
        _success(
            _finding(category="PHONE", start=6, end=11),
            _finding(category="EMAIL", start=0, end=5),
        )
    )

    decision = sanitizer.sanitize_current_user("EMAIL PHONE")

    _assert_skip_content(decision)


def test_should_skip_content_for_non_finding_forged_scan_success() -> None:
    sanitizer, _scanner = _sanitizer(_success("not-a-finding"))

    decision = sanitizer.sanitize_current_user("safe text")

    _assert_skip_content(decision)


def test_should_skip_content_before_sorting_forged_invalid_category() -> None:
    invalid = _finding()
    object.__setattr__(invalid, "category", object())
    sanitizer, _scanner = _sanitizer(_success(invalid))

    decision = sanitizer.sanitize_current_user("EMAIL")

    _assert_skip_content(decision)


def test_should_allow_adjacent_spans() -> None:
    sanitizer, _scanner = _sanitizer(
        _success(
            _finding(category="EMAIL", start=0, end=5),
            _finding(category="PHONE", start=5, end=10),
        )
    )

    decision = sanitizer.sanitize_current_user("EMAILPHONE")

    _assert_store_masked(decision, "[EMAIL][PHONE]")


@pytest.mark.parametrize(
    "spans",
    [
        ((0, 5), (0, 5)),
        ((0, 5), (4, 8)),
        ((0, 8), (2, 5)),
    ],
    ids=["duplicate", "partial-overlap", "contained"],
)
def test_should_skip_content_for_ambiguous_overlapping_spans(spans) -> None:
    findings = tuple(_finding(start=start, end=end) for start, end in spans)
    sanitizer, _scanner = _sanitizer(_success(*findings))

    decision = sanitizer.sanitize_current_user("0123456789")

    _assert_skip_content(decision)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (True, 2),
        (0.0, 2),
        (-1, 2),
        (2, 2),
        (3, 2),
        (0, 11),
    ],
    ids=[
        "boolean",
        "non-integer",
        "negative",
        "empty",
        "reversed",
        "past-end",
    ],
)
def test_should_skip_content_for_invalid_span(start: object, end: object) -> None:
    sanitizer, _scanner = _sanitizer(_success(_finding(start=start, end=end)))

    decision = sanitizer.sanitize_current_user("0123456789")

    _assert_skip_content(decision)


def test_should_skip_content_for_scope_on_non_opt_out_finding() -> None:
    invalid = _finding(category="EMAIL")
    object.__setattr__(invalid, "storage_scope", SimpleNamespace(value="BOTH"))
    sanitizer, _scanner = _sanitizer(_success(invalid))

    decision = sanitizer.sanitize_current_user("EMAIL")

    _assert_skip_content(decision)


def test_should_skip_content_for_unknown_category() -> None:
    invalid = _finding()
    object.__setattr__(invalid, "category", SimpleNamespace(value="DYNAMIC_SECRET"))
    sanitizer, _scanner = _sanitizer(_success(invalid))

    decision = sanitizer.sanitize_current_user("VALUE")

    _assert_skip_content(decision)


def test_should_skip_content_when_required_placeholder_is_missing() -> None:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.history_sanitizer import HistorySanitizer

    policy = resolved_memory_policy().privacy

    class MissingPlaceholderPolicy:
        policy_version = policy.policy_version

        def placeholder_for(self, category):
            return None

    sanitizer = HistorySanitizer(
        StubScanner(_success(_finding(category="EMAIL")), []),
        MissingPlaceholderPolicy(),
    )

    decision = sanitizer.sanitize_current_user("EMAIL")

    _assert_skip_content(decision)


def test_should_convert_scan_failure_to_skip_content() -> None:
    from app.privacy.contracts import ScanFailure, ScanFailureReasonCode

    failure = ScanFailure(
        reason_code=ScanFailureReasonCode.RECOGNIZER_ERROR,
        recognizer_version="contact-v1",
        policy_version=POLICY_VERSION,
    )
    sanitizer, _scanner = _sanitizer(failure)

    decision = sanitizer.sanitize_current_user("synthetic-sensitive-value")

    _assert_skip_content(decision)


@pytest.mark.parametrize("scope", ["HISTORY", "BOTH"])
def test_should_skip_current_user_content_for_history_effective_opt_out(
    scope: str,
) -> None:
    sanitizer, _scanner = _sanitizer(
        _success(
            _finding(
                category="STORAGE_OPT_OUT",
                start=0,
                end=5,
                scope=scope,
            )
        )
    )

    decision = sanitizer.sanitize_current_user("拒否表現です")

    _assert_skip_content(decision)


def test_should_not_block_history_for_rag_only_opt_out() -> None:
    text = "覚えないで"
    sanitizer, _scanner = _sanitizer(
        _success(
            _finding(
                category="STORAGE_OPT_OUT",
                start=0,
                end=len(text),
                scope="RAG",
            )
        )
    )

    decision = sanitizer.sanitize_current_user(text)

    _assert_store_masked(decision, text)


@pytest.mark.parametrize("scope", ["RAG", "HISTORY", "BOTH"])
def test_should_not_activate_storage_opt_out_from_assistant_text(scope: str) -> None:
    text = "保存しないで"
    sanitizer, _scanner = _sanitizer(
        _success(
            _finding(
                category="STORAGE_OPT_OUT",
                start=0,
                end=len(text),
                scope=scope,
            )
        )
    )

    decision = sanitizer.sanitize_assistant(text)

    _assert_store_masked(decision, text)


def test_should_mask_secret_repeated_by_assistant() -> None:
    sanitizer, _scanner = _sanitizer(
        _success(_finding(category="PASSWORD", start=4, end=9))
    )

    decision = sanitizer.sanitize_assistant("復唱: VALUE")

    _assert_store_masked(decision, "復唱: [PASSWORD]")


def test_should_reject_blank_sanitizer_version_during_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy import history_sanitizer

    monkeypatch.setattr(history_sanitizer, "SANITIZER_VERSION", " ")

    with pytest.raises(ValueError, match="version"):
        history_sanitizer.create_history_sanitizer(
            StubScanner(_success(), []),
            resolved_memory_policy().privacy,
        )

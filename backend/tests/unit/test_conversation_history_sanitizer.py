import pytest

from app.conversation_history.models import PrivacySkipReason
from app.conversation_history.sanitizer import (
    REDACTED_PLACEHOLDER,
    SANITIZER_VERSION,
    ConversationHistorySanitizer,
    SanitizedContent,
    SkipContent,
)
from app.conversation_history.scan_models import POLICY_VERSION, RECOGNIZER_VERSION
from app.conversation_history.scanner import DeterministicPrivacyScanner


@pytest.fixture
def sanitizer() -> ConversationHistorySanitizer:
    return ConversationHistorySanitizer(DeterministicPrivacyScanner())


def _sanitized(content: str) -> SanitizedContent:
    return SanitizedContent(
        content,
        RECOGNIZER_VERSION,
        POLICY_VERSION,
        SANITIZER_VERSION,
    )


def _skipped(reason_code: PrivacySkipReason) -> SkipContent:
    return SkipContent(
        reason_code,
        RECOGNIZER_VERSION,
        POLICY_VERSION,
        SANITIZER_VERSION,
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    (
        ("連絡先はperson@example.comです", f"連絡先は{REDACTED_PLACEHOLDER}です"),
        ("password=hunter2 を使っています", f"{REDACTED_PLACEHOLDER} を使っています"),
        ("token sk-abcdefghijklmnop", f"token {REDACTED_PLACEHOLDER}"),
        (
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
            REDACTED_PLACEHOLDER,
        ),
        (
            "seed phrase: one two three four five six seven eight nine ten "
            "eleven twelve",
            REDACTED_PLACEHOLDER,
        ),
        ("CVV: 123", REDACTED_PLACEHOLDER),
        ("電話は090-1234-5678です", f"電話は{REDACTED_PLACEHOLDER}です"),
        (
            "US phone: (415) 555-2671",
            f"US phone: {REDACTED_PLACEHOLDER}",
        ),
        (
            "カードは4111 1111 1111 1111です",
            f"カードは{REDACTED_PLACEHOLDER}です",
        ),
        (
            "電話は090(1234)5678です",
            f"電話は{REDACTED_PLACEHOLDER}です",
        ),
        (
            "電話は０９０（１２３４）５６７８です",
            f"電話は{REDACTED_PLACEHOLDER}です",
        ),
        (
            "カードは4111 (1111) 1111 1111です",
            f"カードは{REDACTED_PLACEHOLDER}です",
        ),
        (
            "token sk - abcdefghijklmnopqrstuvwxyz",
            f"token {REDACTED_PLACEHOLDER}",
        ),
        (
            "マイナンバー 1234-5678-9012",
            REDACTED_PLACEHOLDER,
        ),
        ("SSNは123-45-6789", f"SSNは{REDACTED_PLACEHOLDER}"),
        (
            "driver license number: D1234567",
            REDACTED_PLACEHOLDER,
        ),
        (
            "口座番号: 1234-5678901",
            REDACTED_PLACEHOLDER,
        ),
        (
            "現在地は35.681236, 139.767125です",
            f"現在地は{REDACTED_PLACEHOLDER}です",
        ),
        (
            "〒100-0001 東京都千代田区千代田1-1",
            REDACTED_PLACEHOLDER,
        ),
        (
            "連絡先はｐｅｒｓｏｎ＠ｅｘａｍｐｌｅ．ｃｏｍです",
            f"連絡先は{REDACTED_PLACEHOLDER}です",
        ),
        (
            "連絡先はperson@\u200bexample.comです",
            f"連絡先は{REDACTED_PLACEHOLDER}です",
        ),
    ),
)
def test_should_mask_deterministic_sensitive_values_before_history_persistence(
    sanitizer: ConversationHistorySanitizer,
    content: str,
    expected: str,
) -> None:
    assert sanitizer.sanitize_user_content(content) == _sanitized(expected)
    assert sanitizer.sanitize_assistant_content(content) == _sanitized(expected)


@pytest.mark.parametrize(
    "content",
    (
        "履歴に残さないで、この質問に答えて",
        "この内容は保存しないで",
        "do not save this message",
    ),
)
def test_should_skip_user_content_when_history_storage_is_denied(
    sanitizer: ConversationHistorySanitizer,
    content: str,
) -> None:
    assert sanitizer.sanitize_user_content(content) == _skipped(
        PrivacySkipReason.POLICY_DENIED
    )


def test_should_not_apply_quoted_storage_directive_to_assistant_content(
    sanitizer: ConversationHistorySanitizer,
) -> None:
    content = "「保存しないで」は履歴保存を拒否する指示です"

    assert sanitizer.sanitize_assistant_content(content) == _sanitized(content)


def test_should_keep_rag_only_storage_directive_in_thread_history(
    sanitizer: ConversationHistorySanitizer,
) -> None:
    content = "この内容は覚えないで"

    assert sanitizer.sanitize_user_content(content) == _sanitized(content)


def test_should_skip_empty_or_unrecognizable_history_content(
    sanitizer: ConversationHistorySanitizer,
) -> None:
    assert sanitizer.sanitize_user_content(" ") == _skipped(
        PrivacySkipReason.SENSITIVE_CONTENT
    )
    assert sanitizer.sanitize_user_content("\u200b") == _skipped(
        PrivacySkipReason.SENSITIVE_CONTENT
    )


def test_should_skip_overlapping_findings(
    sanitizer: ConversationHistorySanitizer,
) -> None:
    content = "API key: sk-abcdefghijklmnopqrstuvwxyz"

    assert sanitizer.sanitize_user_content(content) == _skipped(
        PrivacySkipReason.SENSITIVE_CONTENT
    )


def test_should_propagate_scanner_and_sanitizer_versions_in_decision(
    sanitizer: ConversationHistorySanitizer,
) -> None:
    decisions = (
        sanitizer.sanitize_user_content("通常の会話"),
        sanitizer.sanitize_user_content("保存しないで"),
    )

    for decision in decisions:
        assert decision.recognizer_version == RECOGNIZER_VERSION
        assert decision.policy_version == POLICY_VERSION
        assert decision.sanitizer_version == SANITIZER_VERSION

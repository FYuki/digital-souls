from __future__ import annotations

from pathlib import Path
from statistics import median
from time import perf_counter

import pytest

from app.privacy.contracts import PrivacyCategory, StorageScope
from tests.privacy_test_support import (
    POLICY_VERSION,
    load_conformance_cases,
    policy_config,
    write_policy_config,
)


def _scanner():
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.scanner import create_privacy_scanner

    return create_privacy_scanner(resolved_memory_policy().privacy)


def _scanner_from_config(
    config: dict[str, object],
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.memory import memory_policy
    from app.privacy.scanner import create_privacy_scanner

    write_policy_config(path, config)
    monkeypatch.setattr(memory_policy, "MEMORY_POLICY_CONFIG_PATH", path)
    return create_privacy_scanner(memory_policy.resolved_memory_policy().privacy)


def _single_finding(text: str):
    from app.privacy.contracts import ScanSuccess

    result = _scanner().scan(text)
    assert isinstance(result, ScanSuccess)
    assert len(result.findings) == 1
    return result.findings[0]


def _forge_finding(valid, **overrides):
    from app.privacy.contracts import PrivacyFinding

    invalid = object.__new__(PrivacyFinding)
    for field_name in (
        "category",
        "start",
        "end",
        "confidence",
        "reason_code",
        "recognizer_version",
        "policy_version",
        "storage_scope",
    ):
        object.__setattr__(
            invalid,
            field_name,
            overrides.get(field_name, getattr(valid, field_name)),
        )
    return invalid


@pytest.mark.parametrize(
    "case",
    load_conformance_cases(),
    ids=lambda case: case.case_id,
)
def test_should_match_fixed_conformance_corpus_with_original_spans(case) -> None:
    finding = _single_finding(case.text)

    assert finding.category.value == case.category
    assert (finding.start, finding.end) == case.expected_span
    assert finding.storage_scope is None or finding.storage_scope.value == (
        case.storage_scope
    )
    assert finding.recognizer_version == case.recognizer_version
    assert finding.policy_version == case.policy_version


@pytest.mark.parametrize(
    ("text", "category", "matched_text"),
    [
        ("api_key=sk-test_000000000000000000000000", "API_KEY", "sk-test_000000000000000000000000"),
        ("access_token: synthetic-token-000000", "ACCESS_TOKEN", "synthetic-token-000000"),
        ("session_cookie: session-test-000000", "SESSION_COOKIE", "session-test-000000"),
        ("recovery code: 0000-0000", "RECOVERY_CODE", "0000-0000"),
        ("password: synthetic-password", "PASSWORD", "synthetic-password"),
        ("暗証番号: 0000", "PIN", "0000"),
        (
            "-----BEGIN PRIVATE KEY-----\nTESTONLY\n-----END PRIVATE KEY-----",
            "PRIVATE_KEY",
            "-----BEGIN PRIVATE KEY-----\nTESTONLY\n-----END PRIVATE KEY-----",
        ),
        (
            "ssh private key: ssh-ed25519 TESTONLY000000000000",
            "PRIVATE_KEY",
            "ssh-ed25519 TESTONLY000000000000",
        ),
        ("crypto private key: " + "0" * 64, "CRYPTO_PRIVATE_KEY", "0" * 64),
        (
            "seed phrase: alpha bravo charlie delta echo foxtrot golf hotel",
            "SEED_PHRASE",
            "alpha bravo charlie delta echo foxtrot golf hotel",
        ),
        ("CVV: 000", "CVV", "000"),
        ("銀行口座: 0000000", "BANK_ACCOUNT", "0000000"),
        ("routing number: 000000000", "BANK_ACCOUNT", "000000000"),
        ("account number: 0000000000", "BANK_ACCOUNT", "0000000000"),
        ("bank login password: test-only", "BANK_CREDENTIAL", "test-only"),
        ("LINE ID: test-only-user", "PRIVATE_CONTACT", "test-only-user"),
        ("マイナンバー: 000000000000", "GOVERNMENT_ID", "000000000000"),
        ("運転免許証番号: 000000000000", "GOVERNMENT_ID", "000000000000"),
        ("CA DL: Z0000000", "GOVERNMENT_ID", "Z0000000"),
    ],
)
def test_should_detect_each_secret_and_direct_identifier_category(
    text: str,
    category: str,
    matched_text: str,
) -> None:
    finding = _single_finding(text)
    start = text.index(matched_text)

    assert finding.category.value == category
    assert (finding.start, finding.end) == (start, start + len(matched_text))


def test_should_apply_same_scanner_processing_without_role_input() -> None:
    scanner = _scanner()
    text = "連絡先は user@example.test です"

    first = scanner.scan(text)
    second = scanner.scan(text)

    assert first == second


def test_should_return_stably_sorted_findings_from_multiple_recognizers() -> None:
    from app.privacy.contracts import ScanSuccess

    result = _scanner().scan(
        "後方 user@example.test、前方 090-0000-0000、保存しないで"
    )

    assert isinstance(result, ScanSuccess)
    keys = [
        (
            finding.start,
            finding.end,
            finding.category.value,
            finding.reason_code.value,
        )
        for finding in result.findings
    ]
    assert keys == sorted(keys)


@pytest.mark.parametrize(
    ("text", "expected_values"),
    [
        ("first@example.test", ("first@example.test",)),
        ("連絡先:next@example.test", ("next@example.test",)),
        (
            "first@example.test next@example.test",
            ("first@example.test", "next@example.test"),
        ),
    ],
)
def test_should_detect_email_from_each_local_part_boundary(
    text: str,
    expected_values: tuple[str, ...],
) -> None:
    from app.privacy.contracts import ScanSuccess

    result = _scanner().scan(text)

    assert isinstance(result, ScanSuccess)
    email_values = tuple(
        text[finding.start : finding.end]
        for finding in result.findings
        if finding.category is PrivacyCategory.EMAIL
    )
    assert email_values == expected_values


def test_should_scan_long_email_non_match_with_linear_growth() -> None:
    from app.privacy.contracts import ScanSuccess

    scanner = _scanner()

    durations = []
    for size in (10_000, 20_000):
        samples = []
        for _ in range(3):
            started = perf_counter()
            result = scanner.scan("x" * size)
            samples.append(perf_counter() - started)
            assert isinstance(result, ScanSuccess)
            assert result.findings == ()
        durations.append(median(samples))

    assert durations[1] < durations[0] * 3


def test_should_normalize_long_input_with_linear_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.privacy import normalization
    from app.privacy.contracts import ScanSuccess

    scanner = _scanner()
    original_normalize = normalization.unicodedata.normalize
    processed_characters = 0

    def counting_normalize(form: str, value: str) -> str:
        nonlocal processed_characters
        processed_characters += len(value)
        return original_normalize(form, value)

    monkeypatch.setattr(normalization.unicodedata, "normalize", counting_normalize)
    text = "a" * 200

    result = scanner.scan(text)

    assert isinstance(result, ScanSuccess)
    assert processed_characters <= len(text) * 10


def test_should_not_normalize_policy_phrases_during_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.privacy import normalization
    from app.privacy.contracts import ScanSuccess

    scanner = _scanner()
    original_normalize = normalization.unicodedata.normalize
    nfkd_calls = 0

    def counting_normalize(form: str, value: str) -> str:
        nonlocal nfkd_calls
        if form == "NFKD":
            nfkd_calls += 1
        return original_normalize(form, value)

    monkeypatch.setattr(normalization.unicodedata, "normalize", counting_normalize)
    text = "safe input"

    result = scanner.scan(text)

    assert isinstance(result, ScanSuccess)
    assert nfkd_calls == len(text)


def test_should_detect_nfkc_casefold_and_zero_width_obfuscation() -> None:
    finding = _single_finding("ＡＰＩ＿ＫＥＹ＝ＳＫ－ＴＥＳＴ＿００００\u200b００００")

    assert finding.category.value == "API_KEY"
    assert finding.start == len("ＡＰＩ＿ＫＥＹ＝")
    assert finding.end == len("ＡＰＩ＿ＫＥＹ＝ＳＫ－ＴＥＳＴ＿００００\u200b００００")


@pytest.mark.parametrize(
    "text",
    [
        "テストカード 4111-1111-1111-1111",
        "テストカード ４１１１ １１１１ １１１１ １１１１",
        "電話は +81 (90) 0000-0000",
        "Call +1 202-555-0100",
    ],
)
def test_should_detect_compact_formats_without_losing_original_span(text: str) -> None:
    finding = _single_finding(text)

    assert text[finding.start : finding.end]
    assert any(character in text[finding.start : finding.end] for character in " -()")


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("電話は +81 (90) 0000-0000", PrivacyCategory.PHONE),
        ("Call +1 202-555-0100", PrivacyCategory.PHONE),
        ("銀行口座: 0000000", PrivacyCategory.BANK_ACCOUNT),
        ("routing number: 000000000", PrivacyCategory.BANK_ACCOUNT),
        ("account number: 0000000000", PrivacyCategory.BANK_ACCOUNT),
        ("マイナンバー: 000000000000", PrivacyCategory.GOVERNMENT_ID),
        ("運転免許証番号: 000000000000", PrivacyCategory.GOVERNMENT_ID),
        ("SSN: 000-00-0000", PrivacyCategory.GOVERNMENT_ID),
        ("CA DL: Z0000000", PrivacyCategory.GOVERNMENT_ID),
        ("住所は 〒000-0000 東京都千代田区テスト1-1", PrivacyCategory.PRECISE_ADDRESS),
        (
            "Address: 1 Test Street, Example, CA 00000",
            PrivacyCategory.PRECISE_ADDRESS,
        ),
    ],
)
def test_should_not_detect_regional_format_when_policy_rule_is_absent(
    text: str,
    category: PrivacyCategory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.privacy.contracts import ScanSuccess

    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    privacy["regional_patterns"] = []
    scanner = _scanner_from_config(config, tmp_path / "policy.json", monkeypatch)

    result = scanner.scan(text)

    assert isinstance(result, ScanSuccess)
    assert all(finding.category is not category for finding in result.findings)


@pytest.mark.parametrize(
    ("rule", "text", "matched_text"),
    [
        (
            {
                "name": "custom_contact",
                "category": "PHONE",
                "recognizer": "contact",
                "pattern": r"(?P<value>\+819000000000)",
                "view": "compact_phone",
            },
            "電話は +81 (90) 0000-0000",
            "+81 (90) 0000-0000",
        ),
        (
            {
                "name": "custom_financial",
                "category": "BANK_ACCOUNT",
                "recognizer": "financial",
                "pattern": r"custom bank:\s*(?P<value>abc000)",
                "view": "casefold",
            },
            "CUSTOM BANK: AbC000",
            "AbC000",
        ),
        (
            {
                "name": "custom_government",
                "category": "GOVERNMENT_ID",
                "recognizer": "government",
                "pattern": r"CUSTOM-ID:\s*(?P<value>AbC123)",
                "view": "normalized",
            },
            "CUSTOM-ID: AbC123",
            "AbC123",
        ),
        (
            {
                "name": "custom_location",
                "category": "PRECISE_ADDRESS",
                "recognizer": "location",
                "pattern": r"custom address:\s*(?P<value>test place)",
                "view": "casefold",
            },
            "CUSTOM ADDRESS: Test Place",
            "Test Place",
        ),
    ],
)
def test_should_apply_policy_regional_rule_to_owning_recognizer(
    rule: dict[str, str],
    text: str,
    matched_text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.privacy.contracts import ScanSuccess

    config = policy_config()
    privacy = config["privacy"]
    assert isinstance(privacy, dict)
    privacy["regional_patterns"] = [rule]
    scanner = _scanner_from_config(config, tmp_path / "policy.json", monkeypatch)

    result = scanner.scan(text)

    assert isinstance(result, ScanSuccess)
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert text[finding.start : finding.end] == matched_text
    assert finding.category.value == rule["category"]


def test_should_reject_luhn_invalid_card_number() -> None:
    from app.privacy.contracts import ScanSuccess

    result = _scanner().scan("テストカード 4111 1111 1111 1112")

    assert isinstance(result, ScanSuccess)
    assert all(finding.category.value != "PAYMENT_CARD" for finding in result.findings)


@pytest.mark.parametrize(
    "text",
    ["座標は 91.0, 135.0", "座標は 35.0, 181.0"],
)
def test_should_reject_out_of_range_coordinates(text: str) -> None:
    from app.privacy.contracts import ScanSuccess

    result = _scanner().scan(text)

    assert isinstance(result, ScanSuccess)
    assert all(
        finding.category.value != "PRECISE_LOCATION" for finding in result.findings
    )


def test_should_return_invalid_input_failure_without_echoing_input() -> None:
    from app.privacy.contracts import ScanFailure, ScanFailureReasonCode

    result = _scanner().scan(123)

    assert isinstance(result, ScanFailure)
    assert result.reason_code is ScanFailureReasonCode.INVALID_INPUT
    assert "123" not in repr(result)


def test_should_convert_recognizer_exception_to_metadata_only_failure() -> None:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.contracts import ScanFailure, ScanFailureReasonCode
    from app.privacy.scanner import DeterministicPrivacyScanner

    sensitive_text = "synthetic-sensitive-value"

    class FailingRecognizer:
        version = "failing-v1"

        def recognize(self, text: str):
            raise RuntimeError(f"failed on {text}")

    scanner = DeterministicPrivacyScanner(
        resolved_memory_policy().privacy,
        recognizers=(FailingRecognizer(),),
    )

    result = scanner.scan(sensitive_text)

    assert isinstance(result, ScanFailure)
    assert result.reason_code is ScanFailureReasonCode.RECOGNIZER_ERROR
    assert result.recognizer_version == "failing-v1"
    assert sensitive_text not in repr(result)


@pytest.mark.parametrize("recognized", [[], (object(),)])
def test_should_convert_invalid_recognizer_result_shape_to_failure(
    recognized: object,
) -> None:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.contracts import ScanFailure, ScanFailureReasonCode
    from app.privacy.scanner import DeterministicPrivacyScanner

    class InvalidRecognizer:
        version = "invalid-v1"

        def recognize(self, text: str):
            return recognized

    scanner = DeterministicPrivacyScanner(
        resolved_memory_policy().privacy,
        recognizers=(InvalidRecognizer(),),
    )

    result = scanner.scan("safe input")

    assert isinstance(result, ScanFailure)
    assert result.reason_code is ScanFailureReasonCode.INVALID_RECOGNIZER_RESULT
    assert result.recognizer_version == "invalid-v1"
    assert result.policy_version == POLICY_VERSION


@pytest.mark.parametrize(
    "invalid_overrides",
    [
        pytest.param({"category": "EMAIL"}, id="category-type"),
        pytest.param(
            {"reason_code": "DETERMINISTIC_MATCH"},
            id="reason-code-type",
        ),
        pytest.param({"confidence": True}, id="confidence-bool"),
        pytest.param({"confidence": "0.9"}, id="confidence-type"),
        pytest.param({"confidence": float("nan")}, id="confidence-nan"),
        pytest.param({"confidence": float("inf")}, id="confidence-positive-inf"),
        pytest.param({"confidence": float("-inf")}, id="confidence-negative-inf"),
        pytest.param({"confidence": -0.1}, id="confidence-underflow"),
        pytest.param({"confidence": 1.1}, id="confidence-overflow"),
        pytest.param({"start": True}, id="start-bool"),
        pytest.param({"start": "0"}, id="start-type"),
        pytest.param({"start": -1}, id="start-negative"),
        pytest.param({"start": 22}, id="empty-span"),
        pytest.param({"start": 23}, id="reversed-span"),
        pytest.param({"end": True}, id="end-bool"),
        pytest.param({"end": "25"}, id="end-type"),
        pytest.param({"end": 200}, id="end-out-of-range"),
        pytest.param({"recognizer_version": None}, id="recognizer-version-type"),
        pytest.param({"recognizer_version": ""}, id="recognizer-version-empty"),
        pytest.param({"recognizer_version": " "}, id="recognizer-version-blank"),
        pytest.param(
            {"recognizer_version": "different-v1"},
            id="recognizer-version-mismatch",
        ),
        pytest.param({"policy_version": None}, id="policy-version-type"),
        pytest.param({"policy_version": ""}, id="policy-version-empty"),
        pytest.param({"policy_version": " "}, id="policy-version-blank"),
        pytest.param(
            {"policy_version": "different-policy"},
            id="policy-version-mismatch",
        ),
        pytest.param(
            {
                "category": PrivacyCategory.STORAGE_OPT_OUT,
                "storage_scope": None,
            },
            id="opt-out-scope-missing",
        ),
        pytest.param(
            {
                "category": PrivacyCategory.STORAGE_OPT_OUT,
                "storage_scope": "BOTH",
            },
            id="opt-out-scope-type",
        ),
        pytest.param(
            {"storage_scope": StorageScope.BOTH},
            id="non-opt-out-scope-present",
        ),
    ],
)
def test_should_convert_invalid_recognizer_metadata_to_failure(
    invalid_overrides: dict[str, object],
) -> None:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.contracts import ScanFailure, ScanFailureReasonCode
    from app.privacy.scanner import DeterministicPrivacyScanner

    text = "連絡先は user@example.test です"
    metadata = {"recognizer_version": "invalid-metadata-v1", **invalid_overrides}
    invalid = _forge_finding(
        _single_finding(text),
        **metadata,
    )

    class InvalidMetadataRecognizer:
        version = "invalid-metadata-v1"

        def recognize(self, text: str):
            return (invalid,)

    scanner = DeterministicPrivacyScanner(
        resolved_memory_policy().privacy,
        recognizers=(InvalidMetadataRecognizer(),),
    )

    result = scanner.scan(text)

    assert isinstance(result, ScanFailure)
    assert result.reason_code is ScanFailureReasonCode.INVALID_RECOGNIZER_RESULT
    assert result.recognizer_version == "invalid-metadata-v1"
    assert result.policy_version == POLICY_VERSION


def test_should_convert_missing_recognizer_metadata_to_failure() -> None:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.contracts import (
        PrivacyFinding,
        ScanFailure,
        ScanFailureReasonCode,
    )
    from app.privacy.scanner import DeterministicPrivacyScanner

    invalid = object.__new__(PrivacyFinding)

    class MissingMetadataRecognizer:
        version = "missing-metadata-v1"

        def recognize(self, text: str):
            return (invalid,)

    scanner = DeterministicPrivacyScanner(
        resolved_memory_policy().privacy,
        recognizers=(MissingMetadataRecognizer(),),
    )

    result = scanner.scan("safe input")

    assert isinstance(result, ScanFailure)
    assert result.reason_code is ScanFailureReasonCode.INVALID_RECOGNIZER_RESULT
    assert result.recognizer_version == "missing-metadata-v1"
    assert result.policy_version == POLICY_VERSION


@pytest.mark.parametrize("version", [None, "", " "])
def test_should_reject_invalid_recognizer_version_during_construction(
    version: object,
) -> None:
    from app.memory.memory_policy import resolved_memory_policy
    from app.privacy.scanner import DeterministicPrivacyScanner

    class InvalidVersionRecognizer:
        def __init__(self, invalid_version: object) -> None:
            self.version = invalid_version

        def recognize(self, text: str):
            return ()

    with pytest.raises(ValueError, match="version"):
        DeterministicPrivacyScanner(
            resolved_memory_policy().privacy,
            recognizers=(InvalidVersionRecognizer(version),),
        )


def test_should_not_load_policy_file_from_scanner_module() -> None:
    import inspect

    from app.privacy import scanner

    source = inspect.getsource(scanner)
    assert "MEMORY_POLICY_CONFIG_PATH" not in source
    assert "resolved_memory_policy" not in source
    assert ".open(" not in source

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias


class PrivacyCategory(str, Enum):
    API_KEY = "API_KEY"
    ACCESS_TOKEN = "ACCESS_TOKEN"
    SESSION_COOKIE = "SESSION_COOKIE"
    RECOVERY_CODE = "RECOVERY_CODE"
    PASSWORD = "PASSWORD"
    PIN = "PIN"
    PRIVATE_KEY = "PRIVATE_KEY"
    CRYPTO_PRIVATE_KEY = "CRYPTO_PRIVATE_KEY"
    SEED_PHRASE = "SEED_PHRASE"
    PAYMENT_CARD = "PAYMENT_CARD"
    CVV = "CVV"
    BANK_ACCOUNT = "BANK_ACCOUNT"
    BANK_CREDENTIAL = "BANK_CREDENTIAL"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    PRIVATE_CONTACT = "PRIVATE_CONTACT"
    GOVERNMENT_ID = "GOVERNMENT_ID"
    PRECISE_ADDRESS = "PRECISE_ADDRESS"
    PRECISE_LOCATION = "PRECISE_LOCATION"
    STORAGE_OPT_OUT = "STORAGE_OPT_OUT"
    POLICY_ADDED_SENSITIVE = "POLICY_ADDED_SENSITIVE"


class StorageScope(str, Enum):
    RAG = "RAG"
    BOTH = "BOTH"


class FindingReasonCode(str, Enum):
    DETERMINISTIC_MATCH = "DETERMINISTIC_MATCH"
    POLICY_PATTERN_MATCH = "POLICY_PATTERN_MATCH"
    STORAGE_OPT_OUT_MATCH = "STORAGE_OPT_OUT_MATCH"


class ScanFailureReasonCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    RECOGNIZER_ERROR = "RECOGNIZER_ERROR"
    INVALID_RECOGNIZER_RESULT = "INVALID_RECOGNIZER_RESULT"


class ConversationHistoryAction(str, Enum):
    STORE_MASKED = "STORE_MASKED"
    SKIP_CONTENT = "SKIP_CONTENT"


class HistoryDecisionReasonCode(str, Enum):
    UNCHANGED = "UNCHANGED"
    MASKED = "MASKED"
    STORAGE_OPT_OUT = "STORAGE_OPT_OUT"
    SCAN_FAILURE = "SCAN_FAILURE"
    INVALID_FINDING = "INVALID_FINDING"


def _require_version(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty version")


@dataclass(frozen=True, repr=False)
class PrivacyFinding:
    category: PrivacyCategory
    start: int
    end: int
    confidence: float
    reason_code: FindingReasonCode
    recognizer_version: str
    policy_version: str
    storage_scope: StorageScope | None

    def __post_init__(self) -> None:
        if not isinstance(self.category, PrivacyCategory):
            raise TypeError("category must be a PrivacyCategory")
        if not isinstance(self.reason_code, FindingReasonCode):
            raise TypeError("reason_code must be a FindingReasonCode")
        if (
            not isinstance(self.confidence, (int, float))
            or isinstance(self.confidence, bool)
            or not math.isfinite(self.confidence)
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("confidence must be finite and between 0.0 and 1.0")
        _require_version(self.recognizer_version, "recognizer_version")
        _require_version(self.policy_version, "policy_version")
        if self.category is PrivacyCategory.STORAGE_OPT_OUT:
            if not isinstance(self.storage_scope, StorageScope):
                raise ValueError("storage opt-out requires a storage scope")
        elif self.storage_scope is not None:
            raise ValueError("storage scope is only valid for storage opt-out")

    def __repr__(self) -> str:
        return (
            "PrivacyFinding("
            f"category={self.category!r}, confidence={self.confidence!r}, "
            f"reason_code={self.reason_code!r}, "
            f"recognizer_version={self.recognizer_version!r}, "
            f"policy_version={self.policy_version!r}, "
            f"storage_scope={self.storage_scope!r})"
        )


@dataclass(frozen=True)
class ScanSuccess:
    findings: tuple[PrivacyFinding, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.findings, tuple):
            raise TypeError("findings must be a tuple")
        if not all(isinstance(finding, PrivacyFinding) for finding in self.findings):
            raise TypeError("findings must contain only PrivacyFinding values")
        if self.findings != tuple(sorted(self.findings, key=_finding_order_key)):
            raise ValueError("findings must use deterministic order")


def _finding_order_key(
    finding: PrivacyFinding,
) -> tuple[int, int, str, str]:
    return (
        finding.start,
        finding.end,
        finding.category.value,
        finding.reason_code.value,
    )


@dataclass(frozen=True)
class ScanFailure:
    reason_code: ScanFailureReasonCode
    recognizer_version: str
    policy_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason_code, ScanFailureReasonCode):
            raise TypeError("reason_code must be a ScanFailureReasonCode")
        _require_version(self.recognizer_version, "recognizer_version")
        _require_version(self.policy_version, "policy_version")


ScanResult: TypeAlias = ScanSuccess | ScanFailure


class PrivacyScanner(Protocol):
    def scan(self, text: str) -> ScanResult:
        ...


@dataclass(frozen=True, repr=False)
class ConversationHistoryDecision:
    action: ConversationHistoryAction
    reason_code: HistoryDecisionReasonCode
    sanitizer_version: str
    policy_version: str
    content: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.action, ConversationHistoryAction):
            raise TypeError("action must be a ConversationHistoryAction")
        if not isinstance(self.reason_code, HistoryDecisionReasonCode):
            raise TypeError("reason_code must be a HistoryDecisionReasonCode")
        _require_version(self.sanitizer_version, "sanitizer_version")
        _require_version(self.policy_version, "policy_version")
        if self.action is ConversationHistoryAction.STORE_MASKED:
            if not isinstance(self.content, str):
                raise ValueError("STORE_MASKED requires content")
        elif self.content is not None:
            raise ValueError("SKIP_CONTENT must not contain content")

    def __repr__(self) -> str:
        return (
            "ConversationHistoryDecision("
            f"action={self.action!r}, reason_code={self.reason_code!r}, "
            f"sanitizer_version={self.sanitizer_version!r}, "
            f"policy_version={self.policy_version!r})"
        )

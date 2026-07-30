from __future__ import annotations

import math

from app.memory.memory_policy import PrivacyPolicy
from app.privacy.contracts import (
    FindingReasonCode,
    PrivacyCategory,
    PrivacyFinding,
    ScanFailure,
    ScanFailureReasonCode,
    ScanResult,
    ScanSuccess,
    StorageScope,
)
from app.privacy.normalization import build_recognition_views
from app.privacy.recognizers import Recognizer, create_recognizers

SCANNER_VERSION = "privacy-scanner-v1"


class DeterministicPrivacyScanner:
    def __init__(
        self,
        policy: PrivacyPolicy,
        *,
        recognizers: tuple[Recognizer, ...],
    ) -> None:
        if not SCANNER_VERSION.strip():
            raise ValueError("scanner version must be non-empty")
        for recognizer in recognizers:
            if (
                not isinstance(recognizer.version, str)
                or not recognizer.version.strip()
            ):
                raise ValueError("recognizer version must be non-empty")
        self._policy = policy
        self._recognizers = recognizers

    def scan(self, text: str) -> ScanResult:
        if not isinstance(text, str):
            return ScanFailure(
                ScanFailureReasonCode.INVALID_INPUT,
                SCANNER_VERSION,
                self._policy.policy_version,
            )
        findings: list[PrivacyFinding] = []
        try:
            views = build_recognition_views(text)
        except Exception:
            return ScanFailure(
                ScanFailureReasonCode.RECOGNIZER_ERROR,
                SCANNER_VERSION,
                self._policy.policy_version,
            )
        for recognizer in self._recognizers:
            try:
                recognized = recognizer.recognize(views)
            except Exception:
                return ScanFailure(
                    ScanFailureReasonCode.RECOGNIZER_ERROR,
                    recognizer.version,
                    self._policy.policy_version,
                )
            if not self._valid_results(recognized, text, recognizer.version):
                return ScanFailure(
                    ScanFailureReasonCode.INVALID_RECOGNIZER_RESULT,
                    recognizer.version,
                    self._policy.policy_version,
                )
            findings.extend(recognized)
        findings.sort(
            key=lambda finding: (
                finding.start,
                finding.end,
                finding.category.value,
                finding.reason_code.value,
            )
        )
        return ScanSuccess(tuple(findings))

    def _valid_results(
        self,
        findings: object,
        text: str,
        recognizer_version: str,
    ) -> bool:
        if not isinstance(findings, tuple):
            return False
        return all(
            self._valid_finding(finding, text, recognizer_version)
            for finding in findings
        )

    def _valid_finding(
        self,
        finding: object,
        text: str,
        recognizer_version: str,
    ) -> bool:
        if not isinstance(finding, PrivacyFinding):
            return False
        try:
            return (
                isinstance(finding.category, PrivacyCategory)
                and isinstance(finding.reason_code, FindingReasonCode)
                and self._valid_confidence(finding.confidence)
                and self._valid_span(finding.start, finding.end, text)
                and isinstance(finding.recognizer_version, str)
                and bool(finding.recognizer_version.strip())
                and finding.recognizer_version == recognizer_version
                and isinstance(finding.policy_version, str)
                and bool(finding.policy_version.strip())
                and finding.policy_version == self._policy.policy_version
                and self._valid_scope(finding)
            )
        except Exception:
            return False

    @staticmethod
    def _valid_confidence(confidence: object) -> bool:
        return (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and math.isfinite(confidence)
            and 0.0 <= confidence <= 1.0
        )

    @staticmethod
    def _valid_span(start: object, end: object, text: str) -> bool:
        return (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end <= len(text)
        )

    @staticmethod
    def _valid_scope(finding: PrivacyFinding) -> bool:
        if finding.category is PrivacyCategory.STORAGE_OPT_OUT:
            return isinstance(finding.storage_scope, StorageScope)
        return finding.storage_scope is None


def create_privacy_scanner(policy: PrivacyPolicy) -> DeterministicPrivacyScanner:
    return DeterministicPrivacyScanner(
        policy,
        recognizers=create_recognizers(policy),
    )

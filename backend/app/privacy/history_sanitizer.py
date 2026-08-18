from __future__ import annotations

from typing import Protocol

from app.privacy.contracts import (
    ConversationHistoryAction,
    ConversationHistoryDecision,
    FindingReasonCode,
    HistoryDecisionReasonCode,
    PrivacyCategory,
    PrivacyFinding,
    PrivacyScanner,
    ScanFailure,
    ScanSuccess,
    StorageScope,
)

SANITIZER_VERSION = "history-sanitizer-v1"


class _SanitizerPolicy(Protocol):
    @property
    def policy_version(self) -> str:
        ...

    def placeholder_for(self, category: PrivacyCategory) -> str | None:
        ...


class HistorySanitizer:
    def __init__(
        self,
        scanner: PrivacyScanner,
        policy: _SanitizerPolicy,
    ) -> None:
        if not SANITIZER_VERSION.strip():
            raise ValueError("sanitizer version must be non-empty")
        if not policy.policy_version.strip():
            raise ValueError("policy version must be non-empty")
        self._scanner = scanner
        self._policy = policy

    def sanitize_current_user(self, text: str) -> ConversationHistoryDecision:
        return self._sanitize(text, activate_opt_out=True)

    def sanitize_assistant(self, text: str) -> ConversationHistoryDecision:
        return self._sanitize(text, activate_opt_out=False)

    def _sanitize(
        self,
        text: str,
        *,
        activate_opt_out: bool,
    ) -> ConversationHistoryDecision:
        result = self._scanner.scan(text)
        if isinstance(result, ScanFailure):
            return self._skip(HistoryDecisionReasonCode.SCAN_FAILURE)
        if not isinstance(result, ScanSuccess):
            return self._skip(HistoryDecisionReasonCode.SCAN_FAILURE)
        findings = self._validated_findings(result.findings, text)
        if findings is None:
            return self._skip(HistoryDecisionReasonCode.INVALID_FINDING)
        if activate_opt_out and any(
            finding.category is PrivacyCategory.STORAGE_OPT_OUT
            and finding.storage_scope is StorageScope.BOTH
            for finding in findings
        ):
            return self._skip(HistoryDecisionReasonCode.STORAGE_OPT_OUT)

        maskable = tuple(
            finding
            for finding in findings
            if finding.category is not PrivacyCategory.STORAGE_OPT_OUT
        )
        content = text
        for finding in reversed(maskable):
            placeholder = self._policy.placeholder_for(finding.category)
            if placeholder is None:
                return self._skip(HistoryDecisionReasonCode.INVALID_FINDING)
            content = content[: finding.start] + placeholder + content[finding.end :]
        reason = (
            HistoryDecisionReasonCode.MASKED
            if maskable
            else HistoryDecisionReasonCode.UNCHANGED
        )
        return ConversationHistoryDecision(
            action=ConversationHistoryAction.STORE_MASKED,
            reason_code=reason,
            sanitizer_version=SANITIZER_VERSION,
            policy_version=self._policy.policy_version,
            content=content,
        )

    def _validated_findings(
        self,
        findings: object,
        text: str,
    ) -> tuple[PrivacyFinding, ...] | None:
        if not isinstance(findings, tuple) or not all(
            isinstance(finding, PrivacyFinding) for finding in findings
        ):
            return None
        if not all(self._valid_finding(finding, text) for finding in findings):
            return None
        ordered = tuple(
            sorted(
                findings,
                key=lambda finding: (
                    finding.start,
                    finding.end,
                    finding.category.value,
                    finding.reason_code.value,
                ),
            )
        )
        if findings != ordered:
            return None
        previous_end = -1
        for finding in ordered:
            if finding.start < previous_end:
                return None
            previous_end = finding.end
        return ordered

    def _valid_finding(
        self,
        finding: PrivacyFinding,
        text: str,
    ) -> bool:
        if (
            not isinstance(finding.category, PrivacyCategory)
            or not isinstance(finding.reason_code, FindingReasonCode)
            or isinstance(finding.start, bool)
            or isinstance(finding.end, bool)
            or not isinstance(finding.start, int)
            or not isinstance(finding.end, int)
            or finding.start < 0
            or finding.start >= finding.end
            or finding.end > len(text)
            or finding.policy_version != self._policy.policy_version
        ):
            return False
        if finding.category is PrivacyCategory.STORAGE_OPT_OUT:
            return isinstance(finding.storage_scope, StorageScope)
        return (
            finding.storage_scope is None
            and self._policy.placeholder_for(finding.category) is not None
        )

    def _skip(
        self,
        reason_code: HistoryDecisionReasonCode,
    ) -> ConversationHistoryDecision:
        return ConversationHistoryDecision(
            action=ConversationHistoryAction.SKIP_CONTENT,
            reason_code=reason_code,
            sanitizer_version=SANITIZER_VERSION,
            policy_version=self._policy.policy_version,
            content=None,
        )


def create_history_sanitizer(
    scanner: PrivacyScanner,
    policy: _SanitizerPolicy,
) -> HistorySanitizer:
    return HistorySanitizer(scanner, policy)

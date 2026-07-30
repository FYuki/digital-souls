from dataclasses import dataclass

from app.conversation_history.models import PersistedMaskedText, PrivacySkipReason
from app.conversation_history.scan_models import (
    FindingCategory,
    ScanFailure,
    ScanFinding,
    ScanResult,
    StorageScope,
)
from app.conversation_history.scanner import DeterministicPrivacyScanner

REDACTED_PLACEHOLDER = "[REDACTED]"
SANITIZER_VERSION = "history-sanitizer-v1"


@dataclass(frozen=True)
class SanitizedContent:
    content: PersistedMaskedText
    recognizer_version: str
    policy_version: str
    sanitizer_version: str


@dataclass(frozen=True)
class SkipContent:
    reason_code: PrivacySkipReason
    recognizer_version: str
    policy_version: str
    sanitizer_version: str


SanitizationDecision = SanitizedContent | SkipContent


@dataclass(frozen=True)
class ConversationHistorySanitizer:
    scanner: DeterministicPrivacyScanner

    def sanitize_user_content(self, content: str) -> SanitizationDecision:
        return self.decide_user_content(content, self.scanner.scan(content))

    def sanitize_assistant_content(self, content: str) -> SanitizationDecision:
        return self._sanitize(
            content,
            self.scanner.scan(content),
            storage_directives_apply=False,
        )

    def decide_user_content(
        self,
        content: str,
        scan_result: ScanResult,
    ) -> SanitizationDecision:
        return self._sanitize(content, scan_result, storage_directives_apply=True)

    def _sanitize(
        self,
        content: str,
        scan_result: ScanResult,
        *,
        storage_directives_apply: bool,
    ) -> SanitizationDecision:
        if isinstance(scan_result, ScanFailure):
            return _skip_decision(
                PrivacySkipReason.SENSITIVE_CONTENT,
                scan_result,
            )
        skip_reason = _skip_reason(scan_result.findings, storage_directives_apply)
        if skip_reason is not None:
            return _skip_decision(skip_reason, scan_result)

        sanitized = content
        sensitive_findings = tuple(
            finding
            for finding in scan_result.findings
            if finding.category is not FindingCategory.STORAGE_DIRECTIVE
        )
        for finding in reversed(sensitive_findings):
            sanitized = (
                sanitized[: finding.start]
                + REDACTED_PLACEHOLDER
                + sanitized[finding.end :]
            )
        return SanitizedContent(
            content=PersistedMaskedText(sanitized),
            recognizer_version=scan_result.recognizer_version,
            policy_version=scan_result.policy_version,
            sanitizer_version=SANITIZER_VERSION,
        )


def _skip_decision(
    reason_code: PrivacySkipReason,
    scan_result: ScanResult,
) -> SkipContent:
    return SkipContent(
        reason_code=reason_code,
        recognizer_version=scan_result.recognizer_version,
        policy_version=scan_result.policy_version,
        sanitizer_version=SANITIZER_VERSION,
    )


def _skip_reason(
    findings: tuple[ScanFinding, ...],
    storage_directives_apply: bool,
) -> PrivacySkipReason | None:
    previous: ScanFinding | None = None
    for finding in findings:
        if previous is not None and finding.start < previous.end:
            return PrivacySkipReason.SENSITIVE_CONTENT
        previous = finding
        if (
            storage_directives_apply
            and finding.category is FindingCategory.STORAGE_DIRECTIVE
            and finding.storage_scope in (StorageScope.HISTORY, StorageScope.BOTH)
        ):
            return PrivacySkipReason.POLICY_DENIED
    return None

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from app.memory.memory_policy import PatternRule, PrivacyPolicy, StorageOptOutRule
from app.privacy.contracts import (
    FindingReasonCode,
    PrivacyCategory,
    PrivacyFinding,
    StorageScope,
)
from app.privacy.normalization import (
    NormalizedView,
    RecognitionViews,
)


class Recognizer(Protocol):
    version: str

    def recognize(self, views: RecognitionViews) -> tuple[PrivacyFinding, ...]:
        ...


@dataclass(frozen=True)
class _Pattern:
    category: PrivacyCategory
    expression: re.Pattern[str]


class _PatternRecognizer:
    version: str

    def __init__(
        self,
        policy: PrivacyPolicy,
        patterns: tuple[_Pattern, ...],
    ) -> None:
        self._policy = policy
        self._patterns = patterns

    def recognize(self, views: RecognitionViews) -> tuple[PrivacyFinding, ...]:
        view = views.casefold
        findings: list[PrivacyFinding] = []
        for configured in self._patterns:
            for match in configured.expression.finditer(view.text):
                start, end = match.span("value")
                findings.append(
                    _finding(
                        configured.category,
                        view,
                        start,
                        end,
                        self.version,
                        self._policy.policy_version,
                    )
                )
        return tuple(findings)


class CredentialsRecognizer(_PatternRecognizer):
    version = "credentials-v1"

    def __init__(self, policy: PrivacyPolicy) -> None:
        patterns = (
            _pattern(PrivacyCategory.API_KEY, r"api_key\s*[:=]\s*(?P<value>sk-test_[a-z0-9_]{8,})"),
            _pattern(PrivacyCategory.ACCESS_TOKEN, r"access_token\s*[:=]\s*(?P<value>[a-z0-9_-]{12,})"),
            _pattern(PrivacyCategory.SESSION_COOKIE, r"session_cookie\s*[:=]\s*(?P<value>[a-z0-9_-]{12,})"),
            _pattern(PrivacyCategory.RECOVERY_CODE, r"recovery code\s*[:=]\s*(?P<value>\d{4}-\d{4})"),
            _pattern(PrivacyCategory.PASSWORD, r"(?<!bank login )password\s*[:=]\s*(?P<value>\S+)"),
            _pattern(PrivacyCategory.PIN, r"(?:暗証番号|pin)\s*[:=]\s*(?P<value>\d{4,8})"),
        )
        super().__init__(policy, patterns)


class KeysRecognizer(_PatternRecognizer):
    version = "keys-v1"

    def __init__(self, policy: PrivacyPolicy) -> None:
        patterns = (
            _pattern(
                PrivacyCategory.PRIVATE_KEY,
                r"(?P<value>-----begin (?:[a-z]+ )?private key----- .*? "
                r"-----end (?:[a-z]+ )?private key-----)",
            ),
            _pattern(
                PrivacyCategory.PRIVATE_KEY,
                r"ssh private key\s*[:=]\s*(?P<value>ssh-(?:ed25519|rsa) [a-z0-9]+)",
            ),
            _pattern(
                PrivacyCategory.CRYPTO_PRIVATE_KEY,
                r"crypto private key\s*[:=]\s*(?P<value>[0-9a-f]{64})",
            ),
            _pattern(
                PrivacyCategory.SEED_PHRASE,
                r"seed phrase\s*[:=]\s*(?P<value>[a-z]+(?: [a-z]+){7,23})",
            ),
        )
        super().__init__(policy, patterns)


class FinancialRecognizer(_PatternRecognizer):
    version = "financial-v1"

    def __init__(self, policy: PrivacyPolicy) -> None:
        patterns = (
            _pattern(PrivacyCategory.CVV, r"\bcvv\s*[:=]\s*(?P<value>\d{3,4})"),
            _pattern(PrivacyCategory.BANK_CREDENTIAL, r"bank login password\s*[:=]\s*(?P<value>\S+)"),
        )
        super().__init__(policy, patterns)

    def recognize(self, views: RecognitionViews) -> tuple[PrivacyFinding, ...]:
        findings = list(super().recognize(views))
        findings.extend(
            _configured_regional_findings(
                views,
                self._policy,
                "financial",
                self.version,
            )
        )
        view = views.compact_financial
        for match in re.finditer(r"(?<!\d)(?P<value>\d{15,19})(?!\d)", view.text):
            start, end = match.span("value")
            digits = match.group("value")
            if 15 <= len(digits) <= 19 and _passes_luhn(digits):
                findings.append(
                    _finding(
                        PrivacyCategory.PAYMENT_CARD,
                        view,
                        start,
                        end,
                        self.version,
                        self._policy.policy_version,
                    )
                )
        return tuple(findings)


class ContactRecognizer(_PatternRecognizer):
    version = "contact-v1"

    def __init__(self, policy: PrivacyPolicy) -> None:
        patterns = (
            _pattern(
                PrivacyCategory.EMAIL,
                r"(?<![a-z0-9.!#$%&'*+/=?^_`{|}~-])"
                r"(?P<value>[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
                r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)+)",
            ),
            _pattern(
                PrivacyCategory.PRIVATE_CONTACT,
                r"line id\s*[:=]\s*(?P<value>[a-z0-9_-]{4,})",
            ),
        )
        super().__init__(policy, patterns)

    def recognize(self, views: RecognitionViews) -> tuple[PrivacyFinding, ...]:
        findings = list(super().recognize(views))
        findings.extend(
            _configured_regional_findings(
                views,
                self._policy,
                "contact",
                self.version,
            )
        )
        return tuple(findings)


class GovernmentRecognizer(_PatternRecognizer):
    version = "government-v1"

    def __init__(self, policy: PrivacyPolicy) -> None:
        super().__init__(policy, ())

    def recognize(self, views: RecognitionViews) -> tuple[PrivacyFinding, ...]:
        findings = list(super().recognize(views))
        findings.extend(
            _configured_regional_findings(
                views,
                self._policy,
                "government",
                self.version,
            )
        )
        return tuple(findings)


class LocationRecognizer(_PatternRecognizer):
    version = "location-v1"

    def __init__(self, policy: PrivacyPolicy) -> None:
        super().__init__(policy, ())

    def recognize(self, views: RecognitionViews) -> tuple[PrivacyFinding, ...]:
        findings = list(super().recognize(views))
        findings.extend(
            _configured_regional_findings(
                views,
                self._policy,
                "location",
                self.version,
            )
        )
        view = views.casefold
        coordinate = re.compile(
            r"(?<![\d.])(?P<value>-?\d{1,2}(?:\.\d+),\s*"
            r"-?\d{1,3}(?:\.\d+))(?![\d.])"
        )
        for match in coordinate.finditer(view.text):
            latitude_text, longitude_text = match.group("value").split(",", 1)
            if -90 <= float(latitude_text) <= 90 and -180 <= float(longitude_text) <= 180:
                start, end = match.span("value")
                findings.append(
                    _finding(
                        PrivacyCategory.PRECISE_LOCATION,
                        view,
                        start,
                        end,
                        self.version,
                        self._policy.policy_version,
                    )
                )
        return tuple(findings)


class ConfiguredRecognizer:
    version = "configured-v1"

    def __init__(self, policy: PrivacyPolicy) -> None:
        self._policy = policy

    def recognize(self, views: RecognitionViews) -> tuple[PrivacyFinding, ...]:
        findings: list[PrivacyFinding] = []
        for opt_out_rule in self._policy.storage_opt_out_rules:
            findings.extend(
                _opt_out_findings(
                    views.casefold,
                    opt_out_rule,
                    self._policy,
                    self.version,
                )
            )
        for sensitive_rule in self._policy.additional_sensitive_patterns:
            view = (
                views.casefold
                if sensitive_rule.view == "casefold"
                else views.normalized
            )
            for match in sensitive_rule.pattern.finditer(view.text):
                start, end = (
                    match.span("value")
                    if "value" in match.groupdict()
                    else match.span()
                )
                findings.append(
                    _finding(
                        PrivacyCategory.POLICY_ADDED_SENSITIVE,
                        view,
                        start,
                        end,
                        self.version,
                        self._policy.policy_version,
                        reason_code=FindingReasonCode.POLICY_PATTERN_MATCH,
                    )
                )
        return tuple(findings)


def create_recognizers(policy: PrivacyPolicy) -> tuple[Recognizer, ...]:
    available: dict[str, Recognizer] = {
        "credentials": CredentialsRecognizer(policy),
        "keys": KeysRecognizer(policy),
        "financial": FinancialRecognizer(policy),
        "contact": ContactRecognizer(policy),
        "government": GovernmentRecognizer(policy),
        "location": LocationRecognizer(policy),
        "configured": ConfiguredRecognizer(policy),
    }
    return tuple(available[name] for name in policy.required_recognizers)


def _pattern(category: PrivacyCategory, expression: str) -> _Pattern:
    return _Pattern(category, re.compile(expression, re.IGNORECASE))


def _finding(
    category: PrivacyCategory,
    view: NormalizedView,
    start: int,
    end: int,
    recognizer_version: str,
    policy_version: str,
    *,
    storage_scope: StorageScope | None = None,
    reason_code: FindingReasonCode = FindingReasonCode.DETERMINISTIC_MATCH,
) -> PrivacyFinding:
    original_start, original_end = view.original_span(start, end)
    return PrivacyFinding(
        category=category,
        start=original_start,
        end=original_end,
        confidence=1.0,
        reason_code=reason_code,
        recognizer_version=recognizer_version,
        policy_version=policy_version,
        storage_scope=storage_scope,
    )


def _passes_luhn(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _configured_regional_findings(
    views: RecognitionViews,
    policy: PrivacyPolicy,
    recognizer: str,
    version: str,
) -> Iterable[PrivacyFinding]:
    for rule in policy.regional_patterns:
        if rule.recognizer != recognizer:
            continue
        view = _regional_pattern_view(views, rule)
        for match in rule.pattern.finditer(view.text):
            start, end = (
                match.span("value") if "value" in match.groupdict() else match.span()
            )
            yield _finding(
                rule.category,
                view,
                start,
                end,
                version,
                policy.policy_version,
            )


def _regional_pattern_view(
    views: RecognitionViews,
    rule: PatternRule,
) -> NormalizedView:
    if rule.view == "normalized":
        return views.normalized
    if rule.view == "casefold":
        return views.casefold
    if rule.view == "compact_phone":
        return views.compact_phone
    raise ValueError("unsupported regional pattern view")


def _opt_out_findings(
    view: NormalizedView,
    rule: StorageOptOutRule,
    policy: PrivacyPolicy,
    version: str,
) -> Iterable[PrivacyFinding]:
    scope = rule.scope
    for normalized_phrase in rule.normalized_phrases:
        start = 0
        while (match_start := view.text.find(normalized_phrase, start)) >= 0:
            match_end = match_start + len(normalized_phrase)
            yield _finding(
                PrivacyCategory.STORAGE_OPT_OUT,
                view,
                match_start,
                match_end,
                version,
                policy.policy_version,
                storage_scope=scope,
                reason_code=FindingReasonCode.STORAGE_OPT_OUT_MATCH,
            )
            start = match_end
    for pattern in rule.patterns:
        for match in pattern.finditer(view.text):
            start, end = match.span()
            yield _finding(
                PrivacyCategory.STORAGE_OPT_OUT,
                view,
                start,
                end,
                version,
                policy.policy_version,
                storage_scope=scope,
                reason_code=FindingReasonCode.STORAGE_OPT_OUT_MATCH,
            )

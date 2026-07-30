import re
import unicodedata
from dataclasses import dataclass

from app.conversation_history._recognizers import (
    CARD_CANDIDATE,
    PATTERNS,
    STORAGE_DIRECTIVES,
    VENDOR_TOKEN_CANDIDATE,
    VENDOR_TOKEN_COMPACT,
)
from app.conversation_history.scan_models import (
    FindingCategory,
    ScanFailure,
    ScanFinding,
    ScanResult,
    ScanSuccess,
    StorageScope,
)


@dataclass(frozen=True)
class _NormalizedText:
    value: str
    source_spans: tuple[tuple[int, int], ...]

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        return self.source_spans[start][0], self.source_spans[end - 1][1]

    def compact(self, start: int, end: int) -> "_NormalizedText":
        kept = tuple(
            (character, span)
            for character, span in zip(
                self.value[start:end],
                self.source_spans[start:end],
                strict=True,
            )
            if character not in " -()"
        )
        return _NormalizedText(
            "".join(character for character, _span in kept),
            tuple(span for _character, span in kept),
        )


def _normalized_text(text: str) -> _NormalizedText:
    characters: list[str] = []
    spans: list[tuple[int, int]] = []
    for index, source_character in enumerate(text):
        if unicodedata.category(source_character) == "Cf":
            continue
        normalized = unicodedata.normalize("NFKC", source_character)
        for character in normalized:
            characters.append(" " if character.isspace() else character)
            spans.append((index, index + 1))
    return _NormalizedText("".join(characters), tuple(spans))


def _finding(
    normalized: _NormalizedText,
    match: re.Match[str],
    category: FindingCategory,
    reason_code: str,
    storage_scope: StorageScope | None = None,
) -> ScanFinding:
    start, end = normalized.source_span(*match.span())
    return ScanFinding(
        start=start,
        end=end,
        category=category,
        confidence=1.0,
        reason_code=reason_code,
        storage_scope=storage_scope,
    )


def _passes_luhn(candidate: str) -> bool:
    digits = [int(character) for character in candidate if character.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        value = digit * 2 if index % 2 == parity else digit
        total += value - 9 if value > 9 else value
    return total % 10 == 0


class DeterministicPrivacyScanner:
    def scan(self, text: str) -> ScanResult:
        if not text.strip():
            return ScanFailure(reason_code="empty_content")
        normalized = _normalized_text(text)
        if not normalized.value:
            return ScanFailure(reason_code="unrecognizable_content")

        findings = [
            _finding(normalized, match, category, reason_code)
            for category, reason_code, pattern in PATTERNS
            for match in pattern.finditer(normalized.value)
        ]
        findings.extend(
            _finding(
                normalized,
                match,
                FindingCategory.SECRET,
                "credit_card_number",
            )
            for match in CARD_CANDIDATE.finditer(normalized.value)
            if _passes_luhn(match.group())
        )
        findings.extend(
            _finding(
                normalized,
                match,
                FindingCategory.SECRET,
                "vendor_api_key",
            )
            for match in VENDOR_TOKEN_CANDIDATE.finditer(normalized.value)
            if VENDOR_TOKEN_COMPACT.fullmatch(
                normalized.compact(*match.span()).value
            )
        )
        findings.extend(
            _finding(
                normalized,
                match,
                FindingCategory.STORAGE_DIRECTIVE,
                reason_code,
                scope,
            )
            for scope, reason_code, pattern in STORAGE_DIRECTIVES
            for match in pattern.finditer(normalized.value)
        )
        ordered = tuple(
            sorted(
                findings,
                key=lambda item: (
                    item.start,
                    item.end,
                    item.category.value,
                    item.reason_code,
                ),
            )
        )
        return ScanSuccess(findings=ordered)

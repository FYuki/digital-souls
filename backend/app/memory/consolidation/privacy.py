from __future__ import annotations

from app.memory.admission.contracts import MemoryCandidate, MemoryType, StructuredValue
from app.memory.admission.evaluator import RagAdmissionEvaluator
from app.memory.admission.templates import render_normalized_text
from app.privacy.contracts import PrivacyScanner
from app.privacy.semantic.classifier import SemanticPrivacyClassifier
from app.privacy.semantic.contracts import ADMISSION
from app.privacy.semantic.contracts import SemanticClassifierCallProfile

from .contracts import ConsolidationPrivacyReview


class ConsolidationPrivacyReviewer:
    def __init__(
        self,
        *,
        scanner: PrivacyScanner,
        classifier: SemanticPrivacyClassifier,
        evaluator: RagAdmissionEvaluator,
    ) -> None:
        self._scanner = scanner
        self._classifier = classifier
        self._evaluator = evaluator

    def review(
        self,
        *,
        memory_type: MemoryType,
        structured_value: StructuredValue,
        timeout_seconds: float,
    ) -> ConsolidationPrivacyReview:
        candidate = MemoryCandidate(
            memory_type=memory_type,
            structured_value=structured_value,
            source=None,
        )
        slot_scans = {
            key: self._scanner.scan(value)
            for key, value in self._evaluator.slot_values(candidate.structured_value).items()
        }
        normalized_text = render_normalized_text(candidate.structured_value)
        profile = SemanticClassifierCallProfile(
            name=ADMISSION.name,
            timeout_seconds=min(ADMISSION.timeout_seconds, timeout_seconds),
            max_retries=ADMISSION.max_retries,
            retry_backoff_seconds=ADMISSION.retry_backoff_seconds,
            total_timeout_seconds=min(
                ADMISSION.total_timeout_seconds,
                timeout_seconds,
            ),
        )
        assessment = self._classifier.classify(normalized_text, profile)
        result = self._evaluator.evaluate_manual_correction(
            candidate_slot_scans=slot_scans,
            assessment=assessment,
            candidate=candidate,
        )
        return ConsolidationPrivacyReview(result.candidate, assessment)

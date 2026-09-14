"""意味記憶の全slot・根拠・組立後本文を既存privacy契約で検査する。"""

from app.memory.episodic.contracts import FormationStamp
from app.memory.episodic.privacy import PrivacyReview
from app.memory.memory_policy import PrivacyPolicy
from app.memory.semantic.contracts import Proposition
from app.privacy.contracts import PrivacyScanner, ScanSuccess
from app.privacy.semantic.classifier import SemanticPrivacyClassifier
from app.privacy.semantic.contracts import ADMISSION, PrivacyAssessment, SemanticClassification


class SemanticPrivacyReviewer:
    def __init__(
        self, *, scanner: PrivacyScanner, classifier: SemanticPrivacyClassifier, policy: PrivacyPolicy,
    ) -> None:
        self.scanner, self.classifier, self.policy = scanner, classifier, policy

    def review(self, proposition: Proposition, source_texts: tuple[str, ...]) -> PrivacyReview:
        texts = tuple(dict.fromkeys((*source_texts, proposition.subject, proposition.predicate,
                                    proposition.value, proposition.content)))
        if not source_texts:
            return PrivacyReview(False, "MISSING_SOURCE")
        for text in texts:
            result = self.scanner.scan(text)
            if not isinstance(result, ScanSuccess):
                return PrivacyReview(False, "SCAN_FAILED", retryable=True)
            if result.findings or any(value in text for _, value in self.policy.placeholders):
                return PrivacyReview(False, "SENSITIVE_OR_OPT_OUT")
        assessment: PrivacyAssessment | None = None
        for text in dict.fromkeys((*source_texts, proposition.content)):
            assessment = self.classifier.classify(text, ADMISSION)
            if assessment.policy_version != self.policy.policy_version:
                return PrivacyReview(False, "POLICY_VERSION_CHANGED", retryable=True)
            if assessment.classification is SemanticClassification.ABSTAIN:
                return PrivacyReview(False, "SEMANTIC_PRIVACY_UNAVAILABLE", retryable=True)
            if assessment.classification is SemanticClassification.SENSITIVE:
                return PrivacyReview(False, "SEMANTIC_PRIVACY_DENIED")
        assert assessment is not None
        return PrivacyReview(True, "ALLOW", FormationStamp(
            policy_version=assessment.policy_version, classifier_version=assessment.classifier_version,
            model_id=assessment.model_id, model_digest=assessment.model_digest,
            prompt_version=assessment.prompt_version,
        ))

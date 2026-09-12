"""日常経験の重要度を絞らず、全出典と保存する全slotをprivacy検査する。"""

from dataclasses import dataclass

from app.memory.episodic.contracts import FiveW, FormationStamp, RecordKind, text_slots
from app.memory.episodic.rendering import render_five_w
from app.memory.memory_policy import PrivacyPolicy
from app.privacy.contracts import PrivacyScanner, ScanSuccess
from app.privacy.semantic.classifier import SemanticPrivacyClassifier
from app.privacy.semantic.contracts import ADMISSION, PrivacyAssessment, SemanticClassification


@dataclass(frozen=True)
class PrivacyReview:
    allowed: bool
    reason: str
    stamp: FormationStamp | None = None


class EpisodicPrivacyReviewer:
    def __init__(
        self, *, scanner: PrivacyScanner, classifier: SemanticPrivacyClassifier, policy: PrivacyPolicy,
    ) -> None:
        self._scanner = scanner
        self._classifier = classifier
        self._policy = policy
        self._placeholders = tuple(value for _, value in policy.placeholders)

    def review(
        self, *, kind: RecordKind, value: FiveW, source_texts: tuple[str, ...],
    ) -> PrivacyReview:
        if not source_texts:
            return PrivacyReview(False, "MISSING_SOURCE")
        normalized = render_five_w(value, kind=kind)
        texts = tuple(dict.fromkeys((*source_texts, *text_slots(value), normalized)))
        for text in texts:
            result = self._scanner.scan(text)
            if not isinstance(result, ScanSuccess):
                return PrivacyReview(False, "SCAN_FAILED")
            if result.findings or any(token in text for token in self._placeholders):
                return PrivacyReview(False, "SENSITIVE_OR_OPT_OUT")
        assessment: PrivacyAssessment | None = None
        # slotを組み合わせて生成した内容も分類し、出典にない機微情報の生成を通さない。
        for text in dict.fromkeys((*source_texts, normalized)):
            assessment = self._classifier.classify(text, ADMISSION)
            if (
                assessment.policy_version != self._policy.policy_version
                or assessment.classification is not SemanticClassification.NOT_SENSITIVE
            ):
                return PrivacyReview(False, "SEMANTIC_PRIVACY_DENIED")
        assert assessment is not None
        return PrivacyReview(True, "ALLOW", FormationStamp(
            policy_version=assessment.policy_version, classifier_version=assessment.classifier_version,
            model_id=assessment.model_id, model_digest=assessment.model_digest,
            prompt_version=assessment.prompt_version,
        ))

from dataclasses import dataclass
from enum import Enum


class SemanticPrivacyCategory(str, Enum):
    HEALTH = "HEALTH"
    MENTAL_STATE = "MENTAL_STATE"
    SELF_HARM = "SELF_HARM"
    ABUSE_OR_SEXUAL_VIOLENCE = "ABUSE_OR_SEXUAL_VIOLENCE"
    FINANCIAL_SITUATION = "FINANCIAL_SITUATION"
    THIRD_PARTY_PRIVATE = "THIRD_PARTY_PRIVATE"
    OTHER_SENSITIVE = "OTHER_SENSITIVE"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class SemanticClassification(str, Enum):
    SENSITIVE = "SENSITIVE"
    NOT_SENSITIVE = "NOT_SENSITIVE"
    ABSTAIN = "ABSTAIN"


class SubjectScope(str, Enum):
    SELF = "SELF"
    THIRD_PARTY = "THIRD_PARTY"
    GENERAL = "GENERAL"
    UNKNOWN = "UNKNOWN"


class SemanticAssessmentReasonCode(str, Enum):
    SENSITIVE_CONTENT = "SENSITIVE_CONTENT"
    NO_SENSITIVE_CONTENT = "NO_SENSITIVE_CONTENT"
    TIMEOUT = "TIMEOUT"
    MODEL_NOT_LOADED = "MODEL_NOT_LOADED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    UNKNOWN_CATEGORY = "UNKNOWN_CATEGORY"
    UNKNOWN_LANGUAGE = "UNKNOWN_LANGUAGE"


@dataclass(frozen=True)
class PrivacyAssessment:
    classification: SemanticClassification
    subject_scope: SubjectScope
    category: SemanticPrivacyCategory
    reason_code: SemanticAssessmentReasonCode
    classifier_version: str
    model_id: str
    model_digest: str
    prompt_version: str
    policy_version: str

    def __post_init__(self) -> None:
        provenance = (
            self.classifier_version,
            self.model_id,
            self.model_digest,
            self.prompt_version,
            self.policy_version,
        )
        if any(not value.strip() for value in provenance):
            raise ValueError("assessment provenance must not be blank")
        if self.classification is SemanticClassification.SENSITIVE:
            valid = (
                self.subject_scope is not SubjectScope.UNKNOWN
                and self.category
                not in {
                    SemanticPrivacyCategory.NONE,
                    SemanticPrivacyCategory.UNKNOWN,
                }
            )
        elif self.classification is SemanticClassification.NOT_SENSITIVE:
            valid = (
                self.subject_scope is not SubjectScope.UNKNOWN
                and self.category is SemanticPrivacyCategory.NONE
            )
        else:
            valid = (
                self.subject_scope is SubjectScope.UNKNOWN
                and self.category is SemanticPrivacyCategory.UNKNOWN
            )
        expected_reasons = {
            SemanticClassification.SENSITIVE: {
                SemanticAssessmentReasonCode.SENSITIVE_CONTENT
            },
            SemanticClassification.NOT_SENSITIVE: {
                SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT
            },
            SemanticClassification.ABSTAIN: {
                SemanticAssessmentReasonCode.TIMEOUT,
                SemanticAssessmentReasonCode.MODEL_NOT_LOADED,
                SemanticAssessmentReasonCode.MODEL_UNAVAILABLE,
                SemanticAssessmentReasonCode.INVALID_OUTPUT,
                SemanticAssessmentReasonCode.UNKNOWN_CATEGORY,
                SemanticAssessmentReasonCode.UNKNOWN_LANGUAGE,
            },
        }
        valid = valid and self.reason_code in expected_reasons[self.classification]
        if not valid:
            raise ValueError("assessment fields are inconsistent")


@dataclass(frozen=True)
class SemanticClassifierCallProfile:
    timeout_seconds: float
    max_retries: int

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("classifier timeout must be positive")
        if self.max_retries < 0:
            raise ValueError("classifier retry bound must not be negative")


QUERY_GATE = SemanticClassifierCallProfile(timeout_seconds=2.0, max_retries=0)
ADMISSION = SemanticClassifierCallProfile(timeout_seconds=15.0, max_retries=2)

from __future__ import annotations

from collections.abc import Mapping
import json
import logging
import time
from typing import Protocol, TypedDict

import httpx

from app.memory.memory_policy import PrivacyPolicy
from app.privacy.semantic.contracts import (
    PrivacyAssessment,
    SemanticAssessmentReasonCode,
    SemanticClassification,
    SemanticClassifierCallProfile,
    SemanticPrivacyCategory,
    SubjectScope,
)
from app.privacy.semantic.ollama_classifier_client import (
    OllamaInvalidResponseError,
    OllamaModelNotLoadedError,
)


SEMANTIC_CLASSIFIER_VERSION = "semantic-privacy-classifier-v1"
SEMANTIC_PROMPT_VERSION = "semantic-privacy-prompt-v2"
SEMANTIC_FEW_SHOT_TEXTS = (
    "通院の予定が変わって少し不安です",
    "What are common ways to improve sleep hygiene?",
    "友人が借金の返済に困っていると打ち明けた",
    "I prefer tea without sugar.",
)
_RETRYABLE_REASON_CODES = {
    SemanticAssessmentReasonCode.TIMEOUT,
    SemanticAssessmentReasonCode.MODEL_UNAVAILABLE,
}
logger = logging.getLogger(__name__)


class SemanticPrivacyClassifier(Protocol):
    """呼び出し用途別の制限内で意味的な機微情報を分類する公開契約。"""

    def classify(
        self, text: str, profile: SemanticClassifierCallProfile
    ) -> PrivacyAssessment: ...


class SemanticClassifierClient(Protocol):
    """分類器が利用するローカルモデル通信を抽象化する契約。"""

    def chat(
        self,
        messages: tuple[dict[str, str], ...],
        *,
        timeout_seconds: float,
    ) -> str: ...


class _ResponsePayload(TypedDict):
    classification: str
    subject_scope: str
    category: str
    reason_code: str


class OllamaSemanticPrivacyClassifier:
    """固定したpolicyとprovenanceでOllama出力を安全側へ正規化する。"""

    def __init__(
        self,
        *,
        client: SemanticClassifierClient,
        privacy_policy: PrivacyPolicy,
        model_id: str,
        model_digest: str,
    ) -> None:
        self._client = client
        self._policy_version = privacy_policy.policy_version
        self._model_id = model_id
        self._model_digest = model_digest

    def classify(
        self, text: str, profile: SemanticClassifierCallProfile
    ) -> PrivacyAssessment:
        started_at = time.monotonic()
        messages = _build_messages(text)
        final = self._fail_closed(SemanticAssessmentReasonCode.MODEL_UNAVAILABLE)
        attempt_count = 0
        for attempt in range(profile.max_retries + 1):
            if attempt:
                backoff_seconds = profile.retry_backoff_seconds * (2 ** (attempt - 1))
                remaining_seconds = self._remaining_seconds(profile, started_at)
                if remaining_seconds <= backoff_seconds:
                    break
                time.sleep(backoff_seconds)
            remaining_seconds = self._remaining_seconds(profile, started_at)
            if remaining_seconds <= 0:
                break
            attempt_count += 1
            timeout_seconds = (
                profile.timeout_seconds
                if attempt == 0
                else min(profile.timeout_seconds, remaining_seconds)
            )
            final = self._classify_once(
                messages,
                timeout_seconds,
            )
            if (
                final.classification is not SemanticClassification.ABSTAIN
                or final.reason_code not in _RETRYABLE_REASON_CODES
            ):
                break
        elapsed_ms = max(0, round((time.monotonic() - started_at) * 1_000))
        logger.info(
            "Semantic privacy classification completed: "
            "classification=%s reason_code=%s profile=%s "
            "attempt_count=%d elapsed_ms=%d",
            final.classification.value,
            final.reason_code.value,
            profile.name,
            attempt_count,
            elapsed_ms,
        )
        return final

    @staticmethod
    def _remaining_seconds(
        profile: SemanticClassifierCallProfile,
        started_at: float,
    ) -> float:
        return profile.total_timeout_seconds - (time.monotonic() - started_at)

    def _classify_once(
        self,
        messages: tuple[dict[str, str], ...],
        timeout_seconds: float,
    ) -> PrivacyAssessment:
        try:
            raw_output = self._client.chat(
                messages,
                timeout_seconds=timeout_seconds,
            )
            return self._parse(raw_output)
        except (TimeoutError, httpx.TimeoutException):
            return self._fail_closed(SemanticAssessmentReasonCode.TIMEOUT)
        except OllamaModelNotLoadedError:
            return self._fail_closed(
                SemanticAssessmentReasonCode.MODEL_NOT_LOADED
            )
        except OllamaInvalidResponseError:
            return self._fail_closed(SemanticAssessmentReasonCode.INVALID_OUTPUT)
        except Exception:
            return self._fail_closed(SemanticAssessmentReasonCode.MODEL_UNAVAILABLE)

    def _parse(self, raw_output: str) -> PrivacyAssessment:
        try:
            value: object = json.loads(raw_output)
        except (json.JSONDecodeError, TypeError):
            return self._fail_closed(SemanticAssessmentReasonCode.INVALID_OUTPUT)
        payload = _response_payload(value)
        if payload is None:
            return self._fail_closed(SemanticAssessmentReasonCode.INVALID_OUTPUT)
        try:
            category = SemanticPrivacyCategory(payload["category"])
        except ValueError:
            return self._fail_closed(SemanticAssessmentReasonCode.UNKNOWN_CATEGORY)
        try:
            assessment = PrivacyAssessment(
                classification=SemanticClassification(payload["classification"]),
                subject_scope=SubjectScope(payload["subject_scope"]),
                category=category,
                reason_code=SemanticAssessmentReasonCode(payload["reason_code"]),
                classifier_version=SEMANTIC_CLASSIFIER_VERSION,
                model_id=self._model_id,
                model_digest=self._model_digest,
                prompt_version=SEMANTIC_PROMPT_VERSION,
                policy_version=self._policy_version,
            )
        except ValueError:
            return self._fail_closed(SemanticAssessmentReasonCode.INVALID_OUTPUT)
        return assessment

    def _fail_closed(
        self, reason_code: SemanticAssessmentReasonCode
    ) -> PrivacyAssessment:
        return PrivacyAssessment(
            classification=SemanticClassification.ABSTAIN,
            subject_scope=SubjectScope.UNKNOWN,
            category=SemanticPrivacyCategory.UNKNOWN,
            reason_code=reason_code,
            classifier_version=SEMANTIC_CLASSIFIER_VERSION,
            model_id=self._model_id,
            model_digest=self._model_digest,
            prompt_version=SEMANTIC_PROMPT_VERSION,
            policy_version=self._policy_version,
        )


def _response_payload(value: object) -> _ResponsePayload | None:
    required = {"classification", "subject_scope", "category", "reason_code"}
    if not isinstance(value, Mapping) or set(value) != required:
        return None
    classification = value["classification"]
    subject_scope = value["subject_scope"]
    category = value["category"]
    reason_code = value["reason_code"]
    if not (
        isinstance(classification, str)
        and isinstance(subject_scope, str)
        and isinstance(category, str)
        and isinstance(reason_code, str)
    ):
        return None
    return {
        "classification": classification,
        "subject_scope": subject_scope,
        "category": category,
        "reason_code": reason_code,
    }


def _build_messages(text: str) -> tuple[dict[str, str], ...]:
    classification_values = ", ".join(item.value for item in SemanticClassification)
    scope_values = ", ".join(item.value for item in SubjectScope)
    category_values = ", ".join(item.value for item in SemanticPrivacyCategory)
    reason_values = ", ".join(item.value for item in SemanticAssessmentReasonCode)
    system = (
        f"Semantic privacy classifier. Prompt version: {SEMANTIC_PROMPT_VERSION}. "
        "Return exactly one JSON object with classification, subject_scope, category, "
        "and reason_code. "
        f"Allowed classification values: [{classification_values}]. "
        f"Allowed subject_scope values: [{scope_values}]. "
        f"Allowed category values: [{category_values}]. "
        f"Allowed reason_code values: [{reason_values}]. "
        "When the language cannot be understood, use classification=ABSTAIN, "
        "subject_scope=UNKNOWN, category=UNKNOWN, and reason_code=UNKNOWN_LANGUAGE."
    )
    examples = (
        {"role": "user", "content": SEMANTIC_FEW_SHOT_TEXTS[0]},
        {
            "role": "assistant",
            "content": _example_response(
                SemanticClassification.SENSITIVE,
                SubjectScope.SELF,
                SemanticPrivacyCategory.MENTAL_STATE,
                SemanticAssessmentReasonCode.SENSITIVE_CONTENT,
            ),
        },
        {"role": "user", "content": SEMANTIC_FEW_SHOT_TEXTS[1]},
        {
            "role": "assistant",
            "content": _example_response(
                SemanticClassification.NOT_SENSITIVE,
                SubjectScope.GENERAL,
                SemanticPrivacyCategory.NONE,
                SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT,
            ),
        },
        {"role": "user", "content": SEMANTIC_FEW_SHOT_TEXTS[2]},
        {
            "role": "assistant",
            "content": _example_response(
                SemanticClassification.SENSITIVE,
                SubjectScope.THIRD_PARTY,
                SemanticPrivacyCategory.FINANCIAL_SITUATION,
                SemanticAssessmentReasonCode.SENSITIVE_CONTENT,
            ),
        },
        {"role": "user", "content": SEMANTIC_FEW_SHOT_TEXTS[3]},
        {
            "role": "assistant",
            "content": _example_response(
                SemanticClassification.NOT_SENSITIVE,
                SubjectScope.SELF,
                SemanticPrivacyCategory.NONE,
                SemanticAssessmentReasonCode.NO_SENSITIVE_CONTENT,
            ),
        },
    )
    return ({"role": "system", "content": system}, *examples, {"role": "user", "content": text})


def _example_response(
    classification: SemanticClassification,
    subject_scope: SubjectScope,
    category: SemanticPrivacyCategory,
    reason_code: SemanticAssessmentReasonCode,
) -> str:
    return json.dumps(
        {
            "classification": classification.value,
            "subject_scope": subject_scope.value,
            "category": category.value,
            "reason_code": reason_code.value,
        },
        separators=(",", ":"),
    )

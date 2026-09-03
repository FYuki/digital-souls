from __future__ import annotations

from collections.abc import Mapping
import json
import logging
from threading import Lock
import time
from typing import Callable, Protocol, TypedDict

import httpx

from app.inference import InferenceError, InferenceErrorCategory
from app.memory.memory_policy import PrivacyPolicy
from app.privacy.semantic.contracts import (
    PrivacyAssessment,
    SemanticAssessmentReasonCode,
    SemanticClassification,
    SemanticClassifierCallProfile,
    SemanticPrivacyCategory,
    SubjectScope,
)
SEMANTIC_CLASSIFIER_VERSION = "semantic-privacy-classifier-v2"
SEMANTIC_PROMPT_VERSION = "semantic-privacy-prompt-v11"
UNRESOLVED_MODEL_DIGEST = "unresolved"
SEMANTIC_FEW_SHOT_TEXTS = (
    "通院の予定が変わって少し不安です",
    "What are common ways to improve sleep hygiene?",
    "友人が借金の返済に困っていると打ち明けた",
    "I prefer tea without sugar.",
    "弟が急に大切な物を手放し、別れの言葉を繰り返しています",
    "元同僚から望まない接触を受けました",
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


class InferenceSemanticPrivacyClassifier:
    """固定したpolicyとprovenanceでInference出力を安全側へ正規化する。"""

    def __init__(
        self,
        *,
        client: SemanticClassifierClient,
        privacy_policy: PrivacyPolicy,
        model_id: str,
        model_digest: str | None = None,
        model_digest_resolver: Callable[[float], str] | None = None,
    ) -> None:
        if model_digest is None and model_digest_resolver is None:
            raise ValueError("model digest or resolver is required")
        if model_digest is not None and not model_digest.strip():
            raise ValueError("model digest must not be blank")
        self._client = client
        self._policy_version = privacy_policy.policy_version
        self._model_id = model_id
        self._model_digest = model_digest
        self._model_digest_resolver = model_digest_resolver
        self._model_digest_lock = Lock()

    def classify(
        self, text: str, profile: SemanticClassifierCallProfile
    ) -> PrivacyAssessment:
        started_at = time.monotonic()
        digest_failure = self._resolve_model_digest(profile, started_at)
        if digest_failure is not None:
            return self._log_result(
                self._fail_closed(digest_failure),
                profile=profile,
                attempt_count=0,
                started_at=started_at,
            )
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
            timeout_seconds = min(profile.timeout_seconds, remaining_seconds)
            final = self._classify_once(
                messages,
                timeout_seconds,
            )
            if (
                final.classification is not SemanticClassification.ABSTAIN
                or final.reason_code not in _RETRYABLE_REASON_CODES
            ):
                break
        return self._log_result(
            final,
            profile=profile,
            attempt_count=attempt_count,
            started_at=started_at,
        )

    def _resolve_model_digest(
        self,
        profile: SemanticClassifierCallProfile,
        started_at: float,
    ) -> SemanticAssessmentReasonCode | None:
        if self._model_digest is not None:
            return None
        remaining_seconds = self._remaining_seconds(profile, started_at)
        if remaining_seconds <= 0:
            return SemanticAssessmentReasonCode.TIMEOUT
        if not self._model_digest_lock.acquire(timeout=remaining_seconds):
            return SemanticAssessmentReasonCode.TIMEOUT
        try:
            if self._model_digest is not None:
                return (
                    None
                    if self._remaining_seconds(profile, started_at) > 0
                    else SemanticAssessmentReasonCode.TIMEOUT
                )
            resolver = self._model_digest_resolver
            if resolver is None:
                return SemanticAssessmentReasonCode.MODEL_UNAVAILABLE
            remaining_seconds = self._remaining_seconds(profile, started_at)
            if remaining_seconds <= 0:
                return SemanticAssessmentReasonCode.TIMEOUT
            try:
                model_digest = resolver(remaining_seconds)
                if not isinstance(model_digest, str) or not model_digest.strip():
                    return SemanticAssessmentReasonCode.INVALID_OUTPUT
                self._model_digest = model_digest
                if self._remaining_seconds(profile, started_at) <= 0:
                    return SemanticAssessmentReasonCode.TIMEOUT
            except (TimeoutError, httpx.TimeoutException):
                return SemanticAssessmentReasonCode.TIMEOUT
            except InferenceError as error:
                return _inference_failure_reason(error)
            except Exception:
                return SemanticAssessmentReasonCode.MODEL_UNAVAILABLE
        finally:
            self._model_digest_lock.release()
        return None

    @staticmethod
    def _log_result(
        final: PrivacyAssessment,
        *,
        profile: SemanticClassifierCallProfile,
        attempt_count: int,
        started_at: float,
    ) -> PrivacyAssessment:
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
        except InferenceError as error:
            return self._fail_closed(_inference_failure_reason(error))
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
        model_digest = self._model_digest
        if model_digest is None:
            return self._fail_closed(SemanticAssessmentReasonCode.MODEL_UNAVAILABLE)
        try:
            assessment = PrivacyAssessment(
                classification=SemanticClassification(payload["classification"]),
                subject_scope=SubjectScope(payload["subject_scope"]),
                category=category,
                reason_code=SemanticAssessmentReasonCode(payload["reason_code"]),
                classifier_version=SEMANTIC_CLASSIFIER_VERSION,
                model_id=self._model_id,
                model_digest=model_digest,
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
            model_digest=self._model_digest or UNRESOLVED_MODEL_DIGEST,
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


def _inference_failure_reason(error: InferenceError) -> SemanticAssessmentReasonCode:
    if error.category is InferenceErrorCategory.TIMEOUT:
        return SemanticAssessmentReasonCode.TIMEOUT
    if error.category is InferenceErrorCategory.MODEL_NOT_FOUND:
        return SemanticAssessmentReasonCode.MODEL_NOT_LOADED
    if error.category in {
        InferenceErrorCategory.INVALID_RESPONSE,
        InferenceErrorCategory.UNSUPPORTED_CAPABILITY,
    }:
        return SemanticAssessmentReasonCode.INVALID_OUTPUT
    return SemanticAssessmentReasonCode.MODEL_UNAVAILABLE


# 移行期間のimport互換。実装はProvider非依存であり、#181で旧名を撤去する。
OllamaSemanticPrivacyClassifier = InferenceSemanticPrivacyClassifier


def _build_messages(text: str) -> tuple[dict[str, str], ...]:
    classification_values = ", ".join(item.value for item in SemanticClassification)
    scope_values = ", ".join(item.value for item in SubjectScope)
    category_values = ", ".join(item.value for item in SemanticPrivacyCategory)
    reason_values = ", ".join(item.value for item in SemanticAssessmentReasonCode)
    system = (
        f"Semantic privacy classifier. Prompt version: {SEMANTIC_PROMPT_VERSION}. "
        "Do not analyze or explain. Classify immediately. "
        "Classify the text as SENSITIVE only when it states or strongly implies "
        "private information about a specific person. Private means a personal fact; "
        "it does not need to be secret. Personal health, mental state, self-harm, "
        "abuse or sexual violence, and financial facts are SENSITIVE when stated "
        "about the speaker or another specific person. A sensitive topic by itself "
        "is not a personal fact. General questions, requests for general advice, "
        "definitions, and hypothetical statements are NOT_SENSITIVE with "
        "subject_scope=GENERAL and category=NONE, even when they mention health, "
        "mental state, self-harm, abuse, sexual violence, or finances. "
        "Use subject_scope=SELF for private facts about the speaker and "
        "subject_scope=THIRD_PARTY for private facts about another specific person. "
        "The scope is the person described, not the person receiving the disclosure. "
        "Determine subject_scope from the person whose fact or preference is stated, "
        "for both SENSITIVE and NOT_SENSITIVE. First-person statements and preferences "
        "are SELF. When the speaker describes receiving or experiencing an act, use "
        "SELF even if another person performed the act. "
        "In Japanese, when the subject is omitted and the speaker says they received "
        "or experienced an act (for example, ～を受けました or ～されました), use "
        "SELF unless another subject is explicitly stated; a named actor is not the "
        "subject_scope. "
        "In Japanese patterns such as Xは『...』と私に話しました, X is the "
        "described person and the scope is THIRD_PARTY; 私 is only the receiver. "
        "Behavioral warning signs that strongly imply self-harm, abuse, a health or "
        "mental condition, or financial hardship are personal sensitive facts. "
        "Giving away all possessions or settling one's affairs is a SELF_HARM warning "
        "sign and must be classified SENSITIVE even without another warning sign. "
        "Evaluate every clause. If any clause states or strongly implies a personal "
        "sensitive fact, classify the whole text SENSITIVE. A harmless preference in "
        "another clause never cancels treatment, health, mental, abuse, self-harm, or "
        "financial information. "
        "Being under treatment and treatment-related dietary restrictions are "
        "personal HEALTH facts, including Japanese expressions such as 治療中, "
        "治療のため, and 食事制限. "
        "When an understood statement about a specific person is ambiguous between "
        "a sensitive warning sign and a harmless fact, choose SENSITIVE. Use "
        "NOT_SENSITIVE only for clearly harmless facts or clearly general content. "
        "Do not infer an undisclosed private fact from a general question. "
        "Return exactly one JSON object with classification, subject_scope, category, "
        "and reason_code. "
        f"Allowed classification values: [{classification_values}]. "
        f"Allowed subject_scope values: [{scope_values}]. "
        f"Allowed category values: [{category_values}]. "
        f"Allowed reason_code values: [{reason_values}]. "
        "For understood text, never use ABSTAIN. SENSITIVE must use a category other "
        "than NONE or UNKNOWN and reason_code=SENSITIVE_CONTENT. NOT_SENSITIVE must "
        "use category=NONE and reason_code=NO_SENSITIVE_CONTENT. "
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
        {"role": "user", "content": SEMANTIC_FEW_SHOT_TEXTS[4]},
        {
            "role": "assistant",
            "content": _example_response(
                SemanticClassification.SENSITIVE,
                SubjectScope.THIRD_PARTY,
                SemanticPrivacyCategory.SELF_HARM,
                SemanticAssessmentReasonCode.SENSITIVE_CONTENT,
            ),
        },
        {"role": "user", "content": SEMANTIC_FEW_SHOT_TEXTS[5]},
        {
            "role": "assistant",
            "content": _example_response(
                SemanticClassification.SENSITIVE,
                SubjectScope.SELF,
                SemanticPrivacyCategory.ABUSE_OR_SEXUAL_VIOLENCE,
                SemanticAssessmentReasonCode.SENSITIVE_CONTENT,
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

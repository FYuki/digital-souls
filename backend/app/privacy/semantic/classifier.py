from __future__ import annotations

from collections.abc import Mapping
import json
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
SEMANTIC_PROMPT_VERSION = "semantic-privacy-prompt-v1"
SEMANTIC_FEW_SHOT_TEXTS = (
    "通院の予定が変わって少し不安です",
    "What are common ways to improve sleep hygiene?",
    "友人が借金の返済に困っていると打ち明けた",
    "I prefer tea without sugar.",
)


class SemanticPrivacyClassifier(Protocol):
    def classify(
        self, text: str, profile: SemanticClassifierCallProfile
    ) -> PrivacyAssessment: ...


class SemanticClassifierClient(Protocol):
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
        messages = _build_messages(text)
        final = self._fail_closed(SemanticAssessmentReasonCode.MODEL_UNAVAILABLE)
        for _attempt in range(profile.max_retries + 1):
            final = self._classify_once(messages, profile.timeout_seconds)
            if final.classification is not SemanticClassification.ABSTAIN:
                return final
        return final

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
    system = (
        f"Semantic privacy classifier. Prompt version: {SEMANTIC_PROMPT_VERSION}. "
        "Return exactly one JSON object with classification, subject_scope, category, "
        "and reason_code. Use ABSTAIN, UNKNOWN, UNKNOWN, UNKNOWN_LANGUAGE when the "
        "language cannot be understood."
    )
    examples = (
        {"role": "user", "content": SEMANTIC_FEW_SHOT_TEXTS[0]},
        {
            "role": "assistant",
            "content": '{"classification":"SENSITIVE","subject_scope":"SELF",'
            '"category":"MENTAL_STATE","reason_code":"SENSITIVE_CONTENT"}',
        },
        {"role": "user", "content": SEMANTIC_FEW_SHOT_TEXTS[1]},
        {
            "role": "assistant",
            "content": '{"classification":"NOT_SENSITIVE","subject_scope":"GENERAL",'
            '"category":"NONE","reason_code":"NO_SENSITIVE_CONTENT"}',
        },
        {"role": "user", "content": SEMANTIC_FEW_SHOT_TEXTS[2]},
        {
            "role": "assistant",
            "content": '{"classification":"SENSITIVE","subject_scope":"THIRD_PARTY",'
            '"category":"FINANCIAL_SITUATION","reason_code":"SENSITIVE_CONTENT"}',
        },
        {"role": "user", "content": SEMANTIC_FEW_SHOT_TEXTS[3]},
        {
            "role": "assistant",
            "content": '{"classification":"NOT_SENSITIVE","subject_scope":"SELF",'
            '"category":"NONE","reason_code":"NO_SENSITIVE_CONTENT"}',
        },
    )
    return ({"role": "system", "content": system}, *examples, {"role": "user", "content": text})

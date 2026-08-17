from __future__ import annotations

import atexit
from collections.abc import Mapping
import json
import os
import time

from app.memory.memory_policy import resolved_memory_policy
from app.model_settings import resolve_model_settings
from app.privacy.semantic.classifier import (
    OllamaSemanticPrivacyClassifier,
    SemanticClassifierClient,
)
from app.privacy.semantic.contracts import (
    ADMISSION,
    QUERY_GATE,
    SemanticClassifierCallProfile,
)
from app.privacy.semantic.ollama_classifier_client import OllamaClassifierClient


PROFILES = {
    ADMISSION.name: ADMISSION,
    QUERY_GATE.name: QUERY_GATE,
}


class _StubClassifierClient:
    def chat(
        self,
        messages: tuple[dict[str, str], ...],
        *,
        timeout_seconds: float,
    ) -> str:
        del messages, timeout_seconds
        return json.dumps(
            {
                "classification": "SENSITIVE",
                "subject_scope": "SELF",
                "category": "HEALTH",
                "reason_code": "SENSITIVE_CONTENT",
            }
        )

def _classifier() -> OllamaSemanticPrivacyClassifier:
    policy = resolved_memory_policy()
    client: SemanticClassifierClient
    if os.environ.get("PRIVACY_EVAL_STUB") == "1":
        client = _StubClassifierClient()
        model_id = "semantic-privacy-eval-stub"
        model_digest = "sha256:semantic-privacy-eval-stub"
    else:
        settings = resolve_model_settings(os.environ)
        real_client = OllamaClassifierClient(
            model_id=settings.ollama_classifier_model
        )
        model_id = settings.ollama_classifier_model
        model_digest = real_client.resolve_model_digest()
        client = real_client
        atexit.register(real_client.close)
    return OllamaSemanticPrivacyClassifier(
        client=client,
        privacy_policy=policy.privacy,
        model_id=model_id,
        model_digest=model_digest,
    )


CLASSIFIER = _classifier()


def _profile(options: Mapping[str, object]) -> SemanticClassifierCallProfile:
    config = options.get("config")
    if not isinstance(config, Mapping):
        raise ValueError("provider config is required")
    name = config.get("profile")
    if not isinstance(name, str) or name not in PROFILES:
        raise ValueError("provider profile must be ADMISSION or QUERY_GATE")
    return PROFILES[name]


def _case_id(context: Mapping[str, object]) -> str:
    variables = context.get("vars")
    if not isinstance(variables, Mapping):
        raise ValueError("test vars are required")
    case_id = variables.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case_id must be a non-empty string")
    return case_id


def call_api(
    prompt: str,
    options: dict[str, object],
    context: dict[str, object],
) -> dict[str, object]:
    profile = _profile(options)
    case_id = _case_id(context)
    started_at = time.monotonic()
    assessment = CLASSIFIER.classify(prompt, profile)
    latency_seconds = time.monotonic() - started_at
    return {
        "output": json.dumps(
            {
                "case_id": case_id,
                "profile": profile.name,
                "classification": assessment.classification.value,
                "subject_scope": assessment.subject_scope.value,
                "category": assessment.category.value,
                "reason_code": assessment.reason_code.value,
                "classifier_version": assessment.classifier_version,
                "model_id": assessment.model_id,
                "model_digest": assessment.model_digest,
                "prompt_version": assessment.prompt_version,
                "policy_version": assessment.policy_version,
                "latency_seconds": latency_seconds,
            },
            ensure_ascii=False,
        ),
        "latencyMs": round(latency_seconds * 1_000),
    }

from __future__ import annotations

import json
import os

from app.memory.memory_policy import resolved_memory_policy
from app.model_settings import resolve_model_settings
from app.privacy.semantic.classifier import OllamaSemanticPrivacyClassifier
from app.privacy.semantic.contracts import ADMISSION
from app.privacy.semantic.ollama_classifier_client import OllamaClassifierClient


def _classifier() -> OllamaSemanticPrivacyClassifier:
    policy = resolved_memory_policy()
    settings = resolve_model_settings(os.environ)
    client = OllamaClassifierClient(model_id=settings.ollama_chat_model)
    return OllamaSemanticPrivacyClassifier(
        client=client,
        privacy_policy=policy.privacy,
        model_id=settings.ollama_chat_model,
        model_digest=client.resolve_model_digest(),
    )


CLASSIFIER = _classifier()


def call_api(
    prompt: str,
    options: dict[str, object],
    context: dict[str, object],
) -> dict[str, str]:
    del options, context
    assessment = CLASSIFIER.classify(prompt, ADMISSION)
    return {
        "output": json.dumps(
            {
                "classification": assessment.classification.value,
                "subject_scope": assessment.subject_scope.value,
                "category": assessment.category.value,
                "reason_code": assessment.reason_code.value,
                "classifier_version": assessment.classifier_version,
                "model_id": assessment.model_id,
                "model_digest": assessment.model_digest,
                "prompt_version": assessment.prompt_version,
                "policy_version": assessment.policy_version,
            },
            ensure_ascii=False,
        )
    }

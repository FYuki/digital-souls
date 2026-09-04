from __future__ import annotations

import atexit
from collections.abc import Mapping
import json
import os
import time

from app.memory.memory_policy import resolved_memory_policy
from app.inference import InferenceTarget
from app.inference.runtime import create_inference_runtime
from app.privacy.semantic.classifier import (
    InferenceSemanticPrivacyClassifier,
    SemanticClassifierClient,
)
from app.privacy.semantic.contracts import (
    ADMISSION,
    QUERY_GATE,
    SemanticClassifierCallProfile,
)
from app.privacy.semantic.inference_client import InferenceSemanticClassifierClient


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

def _classifier() -> InferenceSemanticPrivacyClassifier:
    policy = resolved_memory_policy()
    client: SemanticClassifierClient
    if os.environ.get("PRIVACY_EVAL_STUB") == "1":
        client = _StubClassifierClient()
        model_id = "semantic-privacy-eval-stub"
        model_digest = "sha256:semantic-privacy-eval-stub"
    else:
        runtime = create_inference_runtime(os.environ)
        target = runtime.settings.target(InferenceTarget.PRIVACY)
        if target.reference.provider_id != "ollama":
            runtime.close()
            raise ValueError("privacy evaluation requires the Ollama provider")
        real_client = InferenceSemanticClassifierClient(
            router=runtime.router,
            settings=runtime.settings,
            model_digest_resolver=lambda model_id, timeout_seconds: (
                runtime.ollama_adapter.resolve_model_digest(
                    model_id,
                    timeout_seconds=timeout_seconds,
                )
            ),
        )
        model_id = target.reference.model_id
        model_digest = real_client.resolve_model_digest(timeout_seconds=10.0)
        client = real_client
        atexit.register(runtime.close)
    return InferenceSemanticPrivacyClassifier(
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

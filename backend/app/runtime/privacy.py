"""privacy policy・scanner・sanitizer・semantic classifierを所有する。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.inference import InferenceTarget
from app.inference.runtime import target_model_id
from app.memory.memory_policy import MemoryPolicy
from app.privacy.contracts import PrivacyScanner
from app.privacy.history_sanitizer import HistorySanitizer, create_history_sanitizer
from app.privacy.scanner import create_privacy_scanner
from app.privacy.semantic.classifier import InferenceSemanticPrivacyClassifier
from app.privacy.semantic.inference_client import InferenceSemanticClassifierClient

if TYPE_CHECKING:
    from fastapi import FastAPI
    from app.runtime.inference import InferenceResources


class PrivacyResources:
    """policy・scanner・sanitizer・semantic classifierの資源owner。"""

    def __init__(self, policy: MemoryPolicy) -> None:
        self.policy = policy
        self.scanner: PrivacyScanner | None = None
        self.history_sanitizer: HistorySanitizer | None = None
        self.classifier_client: InferenceSemanticClassifierClient | None = None
        self.classifier: InferenceSemanticPrivacyClassifier | None = None
        self.classifier_published = False

    def build_scanner_sanitizer(self) -> None:
        self.scanner = create_privacy_scanner(self.policy.privacy)
        self.history_sanitizer = create_history_sanitizer(
            self.scanner, self.policy.privacy
        )

    def build_classifier(self, inference: InferenceResources) -> None:
        runtime = inference.runtime
        self.classifier_client = InferenceSemanticClassifierClient(
            router=runtime.router,
            settings=runtime.settings,
            model_digest_resolver=lambda model_id, timeout_seconds: (
                runtime.ollama_adapter.resolve_model_digest(
                    model_id,
                    timeout_seconds=timeout_seconds,
                )
            ),
        )
        classifier_client = self.classifier_client
        self.classifier = InferenceSemanticPrivacyClassifier(
            client=classifier_client,
            privacy_policy=self.policy.privacy,
            model_id=target_model_id(
                runtime.settings,
                InferenceTarget.PRIVACY,
            ),
            model_digest_resolver=lambda timeout_seconds: (
                classifier_client.resolve_model_digest(
                    timeout_seconds=timeout_seconds
                )
            ),
        )

    def publish(self, app: FastAPI) -> None:
        app.state.semantic_privacy_classifier = self.classifier
        self.classifier_published = True

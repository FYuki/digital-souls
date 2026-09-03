from __future__ import annotations

from collections.abc import Callable
import json

from app.inference import (
    InferenceCaller,
    InferenceMessage,
    InferenceRouter,
    InferenceSettings,
    InferenceTarget,
)
from app.privacy.semantic.schema import SEMANTIC_RESPONSE_SCHEMA


class InferenceSemanticClassifierClient:
    """Semantic Privacyのdomain契約をInference Targetへ接続する。"""

    def __init__(
        self,
        *,
        router: InferenceRouter,
        settings: InferenceSettings,
        model_digest_resolver: Callable[[str, float], str],
    ) -> None:
        self._router = router
        self._model_id = settings.target(InferenceTarget.PRIVACY).reference.model_id
        self._model_digest_resolver = model_digest_resolver
        self._caller = InferenceCaller.SEMANTIC_PRIVACY

    @property
    def model_id(self) -> str:
        return self._model_id

    def close(self) -> None:
        return None

    def chat(
        self,
        messages: tuple[dict[str, str], ...],
        *,
        timeout_seconds: float,
    ) -> str:
        result = self._router.generate_structured(
            caller=self._caller,
            target=InferenceTarget.PRIVACY,
            messages=tuple(
                InferenceMessage(message["role"], message["content"])
                for message in messages
            ),
            response_schema=SEMANTIC_RESPONSE_SCHEMA,
            timeout_seconds=timeout_seconds,
        )
        return json.dumps(
            result.value,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def resolve_model_digest(self, *, timeout_seconds: float) -> str:
        return self._model_digest_resolver(self._model_id, timeout_seconds)

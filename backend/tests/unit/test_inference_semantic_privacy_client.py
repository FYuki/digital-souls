from __future__ import annotations

from unittest.mock import MagicMock

from app.inference import InferenceTarget, default_provider_registry
from app.inference.config import resolve_inference_settings
from app.inference.contracts import StructuredGenerationResult
from app.privacy.semantic.inference_client import InferenceSemanticClassifierClient


def _settings():
    environment: dict[str, str] = {}
    for token in (
        "CHAT",
        "PRIVACY",
        "MEMORY_EXTRACTION",
        "MEMORY_CONSOLIDATION",
    ):
        environment[f"INFERENCE_TARGET_{token}"] = "ollama/gemma4:e4b"
        environment[f"INFERENCE_TARGET_{token}_MAX_INPUT_TOKENS"] = "7168"
        environment[f"INFERENCE_TARGET_{token}_MAX_OUTPUT_TOKENS"] = "1024"
    environment["INFERENCE_TARGET_EMBEDDING"] = "ollama/nomic-embed-text:latest"
    environment["INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS"] = "8192"
    return resolve_inference_settings(environment, default_provider_registry())


def test_semantic_client_uses_privacy_target_and_returns_canonical_json() -> None:
    router = MagicMock()
    router.generate_structured.return_value = StructuredGenerationResult(
        value={"classification": "ABSTAIN"},
        usage=None,
    )
    client = InferenceSemanticClassifierClient(
        router=router,
        settings=_settings(),
        model_digest_resolver=lambda _model, _timeout: "sha256:synthetic",
    )

    result = client.chat(
        ({"role": "user", "content": "private input"},),
        timeout_seconds=2.0,
    )

    assert result == '{"classification":"ABSTAIN"}'
    assert router.generate_structured.call_args.kwargs["target"] is InferenceTarget.PRIVACY
    assert router.generate_structured.call_args.kwargs["timeout_seconds"] == 2.0
    assert client.model_id == "gemma4:e4b"


def test_semantic_client_delegates_digest_without_exposing_credentials() -> None:
    calls: list[tuple[str, float]] = []
    client = InferenceSemanticClassifierClient(
        router=MagicMock(),
        settings=_settings(),
        model_digest_resolver=lambda model, timeout: (
            calls.append((model, timeout)) or "sha256:synthetic"
        ),
    )

    assert client.resolve_model_digest(timeout_seconds=1.5) == "sha256:synthetic"
    assert calls == [("gemma4:e4b", 1.5)]

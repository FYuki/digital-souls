from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging

from app.inference.adapters.ollama import OllamaAdapter
from app.inference.authorization import InferenceAuthorizer
from app.inference.config import (
    INFERENCE_TARGET_PREFIX,
    InferenceSettings,
    resolve_inference_settings,
)
from app.inference.contracts import InferenceTarget
from app.inference.registry import ProviderRegistry, default_provider_registry
from app.inference.router import InferenceRouter
from app.llm.ollama_config import (
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_EMBEDDING_MODEL,
    OLLAMA_BASE_URL_ENV,
    OLLAMA_EMBEDDING_MODEL_ENV,
)
from app.memory.formation.config import resolve_memory_formation_settings
from app.model_settings import (
    OLLAMA_CHAT_MODEL_ENV,
    OLLAMA_CLASSIFIER_MODEL_ENV,
    OLLAMA_CONTEXT_TOKENS_ENV,
    OLLAMA_EXTRACTOR_MODEL_ENV,
    OLLAMA_RESPONSE_RESERVE_TOKENS_ENV,
    resolve_model_settings,
)


logger = logging.getLogger(__name__)
LEGACY_INFERENCE_ENV_KEYS = frozenset(
    {
        OLLAMA_CHAT_MODEL_ENV,
        OLLAMA_CLASSIFIER_MODEL_ENV,
        OLLAMA_EXTRACTOR_MODEL_ENV,
        OLLAMA_EMBEDDING_MODEL_ENV,
        OLLAMA_CONTEXT_TOKENS_ENV,
        OLLAMA_RESPONSE_RESERVE_TOKENS_ENV,
    }
)


@dataclass(frozen=True)
class InferenceRuntime:
    settings: InferenceSettings
    registry: ProviderRegistry
    router: InferenceRouter
    ollama_adapter: OllamaAdapter

    def close(self) -> None:
        self.ollama_adapter.close()


def create_inference_runtime(environment: Mapping[str, str]) -> InferenceRuntime:
    registry = default_provider_registry()
    resolved_environment = transition_inference_environment(environment)
    settings = resolve_inference_settings(resolved_environment, registry)
    providers = {
        resolved.reference.provider_id for resolved in settings.targets.values()
    }
    unavailable = providers - {"ollama"}
    if unavailable:
        raise ValueError(
            f"configured inference provider is not available: {sorted(unavailable)[0]}"
        )
    ollama_adapter = OllamaAdapter(
        base_url=environment.get(OLLAMA_BASE_URL_ENV, DEFAULT_OLLAMA_BASE_URL)
    )
    registry.bind(ollama_adapter)
    router = InferenceRouter(
        settings=settings,
        registry=registry,
        authorizer=InferenceAuthorizer(),
    )
    return InferenceRuntime(settings, registry, router, ollama_adapter)


def transition_inference_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    has_new = any(key.startswith(INFERENCE_TARGET_PREFIX) for key in environment)
    explicit_legacy = sorted(LEGACY_INFERENCE_ENV_KEYS & set(environment))
    if has_new and explicit_legacy:
        raise ValueError(
            "new and legacy inference settings must not be configured together"
        )
    if has_new:
        return dict(environment)

    logger.warning(
        "Legacy Ollama inference settings are deprecated; migrate to INFERENCE_TARGET_*"
    )
    model = resolve_model_settings(environment)
    formation = resolve_memory_formation_settings(environment)
    context = model.ollama_context_tokens
    chat_output = model.assistant_max_generation_tokens
    structured_output = formation.max_output_tokens
    if structured_output >= context:
        raise ValueError("memory formation output limit must be below model context")
    values = dict(environment)
    target_values = {
        "CHAT": model.ollama_chat_model,
        "PRIVACY": model.ollama_classifier_model,
        "MEMORY_EXTRACTION": model.ollama_extractor_model,
        "MEMORY_CONSOLIDATION": model.ollama_extractor_model,
        "EMBEDDING": environment.get(
            OLLAMA_EMBEDDING_MODEL_ENV,
            DEFAULT_OLLAMA_EMBEDDING_MODEL,
        ),
    }
    for token, model_id in target_values.items():
        values[f"{INFERENCE_TARGET_PREFIX}{token}"] = f"ollama/{model_id}"
    values[f"{INFERENCE_TARGET_PREFIX}CHAT_MAX_INPUT_TOKENS"] = str(
        context - chat_output
    )
    values[f"{INFERENCE_TARGET_PREFIX}CHAT_MAX_OUTPUT_TOKENS"] = str(chat_output)
    for token in ("PRIVACY", "MEMORY_EXTRACTION", "MEMORY_CONSOLIDATION"):
        values[f"{INFERENCE_TARGET_PREFIX}{token}_MAX_INPUT_TOKENS"] = str(
            context - structured_output
        )
        values[f"{INFERENCE_TARGET_PREFIX}{token}_MAX_OUTPUT_TOKENS"] = str(
            structured_output
        )
    values[f"{INFERENCE_TARGET_PREFIX}EMBEDDING_MAX_INPUT_TOKENS"] = str(context)
    values[f"{INFERENCE_TARGET_PREFIX}PRIVACY_TIMEOUT_SECONDS"] = "15"
    values[f"{INFERENCE_TARGET_PREFIX}MEMORY_EXTRACTION_TIMEOUT_SECONDS"] = str(
        formation.llm_timeout_seconds
    )
    values[f"{INFERENCE_TARGET_PREFIX}MEMORY_CONSOLIDATION_TIMEOUT_SECONDS"] = "15"
    return values


def target_model_id(settings: InferenceSettings, target: InferenceTarget) -> str:
    return settings.target(target).reference.model_id

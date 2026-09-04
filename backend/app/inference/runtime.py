from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil

from app.inference.adapters.ollama import OllamaAdapter
from app.inference.adapters.openai_api import OpenAIAPIAdapter
from app.inference.adapters.openai_codex import OpenAICodexAdapter
from app.inference.config import (
    INFERENCE_TARGET_PREFIX,
    InferenceSettings,
    resolve_inference_settings,
)
from app.inference.contracts import InferenceTarget, ProviderKind
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
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_CODEX_HOME_ENV = "OPENAI_CODEX_HOME"
OPENAI_CODEX_EXECUTABLE_ENV = "OPENAI_CODEX_EXECUTABLE"
FORBIDDEN_OPENAI_ENDPOINT_ENV_KEYS = frozenset(
    {"OPENAI_API_BASE", "OPENAI_API_ENDPOINT", "OPENAI_BASE_URL"}
)
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
    openai_api_adapter: OpenAIAPIAdapter | None = None
    openai_codex_adapter: OpenAICodexAdapter | None = None

    def close(self) -> None:
        if self.openai_codex_adapter is not None:
            self.openai_codex_adapter.close()
        if self.openai_api_adapter is not None:
            self.openai_api_adapter.close()
        self.ollama_adapter.close()


def create_inference_runtime(environment: Mapping[str, str]) -> InferenceRuntime:
    registry = default_provider_registry()
    resolved_environment = transition_inference_environment(environment)
    settings = resolve_inference_settings(resolved_environment, registry)
    providers = {
        resolved.reference.provider_id for resolved in settings.targets.values()
    }
    api_key: str | None = None
    if "openai-api" in providers:
        forbidden_endpoint_keys = sorted(
            FORBIDDEN_OPENAI_ENDPOINT_ENV_KEYS & set(environment)
        )
        if forbidden_endpoint_keys:
            raise ValueError(
                f"openai-api endpoint override is forbidden: {forbidden_endpoint_keys[0]}"
            )
        api_key = environment.get(OPENAI_API_KEY_ENV)
        if api_key is None or not api_key or api_key.strip() != api_key:
            raise ValueError("OPENAI_API_KEY is required when openai-api is selected")

    codex_home: Path | None = None
    executable_path: Path | None = None
    if "openai-codex" in providers:
        raw_codex_home = environment.get(OPENAI_CODEX_HOME_ENV)
        if raw_codex_home is None:
            raise ValueError(
                "OPENAI_CODEX_HOME is required when openai-codex is selected"
            )
        codex_home = Path(raw_codex_home)
        if not codex_home.is_absolute() or not codex_home.is_dir():
            raise ValueError("OPENAI_CODEX_HOME must be an existing absolute directory")
        raw_executable = environment.get(OPENAI_CODEX_EXECUTABLE_ENV)
        executable = (
            shutil.which("codex", path=environment.get("PATH"))
            if raw_executable is None
            else raw_executable
        )
        if executable is None:
            raise ValueError(
                "Codex executable is required when openai-codex is selected"
            )
        executable_path = Path(executable)
        if (
            not executable_path.is_absolute()
            or not executable_path.is_file()
            or not os.access(executable_path, os.X_OK)
        ):
            raise ValueError(
                "OPENAI_CODEX_EXECUTABLE must be an existing absolute file"
            )
    ollama_adapter = OllamaAdapter(
        base_url=environment.get(OLLAMA_BASE_URL_ENV, DEFAULT_OLLAMA_BASE_URL)
    )
    openai_api_adapter: OpenAIAPIAdapter | None = None
    openai_codex_adapter: OpenAICodexAdapter | None = None
    try:
        if "ollama" in providers:
            registry.bind(ollama_adapter)
        if api_key is not None:
            openai_api_adapter = OpenAIAPIAdapter(api_key=api_key)
            registry.bind(openai_api_adapter)
        if codex_home is not None and executable_path is not None:
            openai_codex_adapter = OpenAICodexAdapter(
                executable=executable_path,
                codex_home=codex_home,
                inherited_environment=environment,
            )
            registry.bind(openai_codex_adapter)
    except Exception:
        if openai_codex_adapter is not None:
            openai_codex_adapter.close()
        if openai_api_adapter is not None:
            openai_api_adapter.close()
        ollama_adapter.close()
        raise

    for target, resolved in settings.targets.items():
        descriptor = registry.descriptor(resolved.reference.provider_id)
        if descriptor.kind is ProviderKind.CLOUD:
            logger.warning(
                "Cloud inference configured target=%s provider=%s model=%s",
                target.value,
                resolved.reference.provider_id,
                resolved.reference.model_id,
            )
    router = InferenceRouter(
        settings=settings,
        registry=registry,
    )
    return InferenceRuntime(
        settings,
        registry,
        router,
        ollama_adapter,
        openai_api_adapter,
        openai_codex_adapter,
    )


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
        values[f"{INFERENCE_TARGET_PREFIX}{token}_OPTIONS_JSON"] = '{"temperature":0}'
    values[f"{INFERENCE_TARGET_PREFIX}EMBEDDING_MAX_INPUT_TOKENS"] = str(context)
    values[f"{INFERENCE_TARGET_PREFIX}PRIVACY_TIMEOUT_SECONDS"] = "15"
    values[f"{INFERENCE_TARGET_PREFIX}MEMORY_EXTRACTION_TIMEOUT_SECONDS"] = str(
        formation.llm_timeout_seconds
    )
    values[f"{INFERENCE_TARGET_PREFIX}MEMORY_CONSOLIDATION_TIMEOUT_SECONDS"] = "15"
    return values


def target_model_id(settings: InferenceSettings, target: InferenceTarget) -> str:
    return settings.target(target).reference.model_id

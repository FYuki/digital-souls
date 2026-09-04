from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil

from app.inference.adapters.ollama import OllamaAdapter
from app.inference.adapters.openai_api import OpenAIAPIAdapter
from app.inference.adapters.openai_codex import OpenAICodexAdapter
from app.inference.config import (
    InferenceSettings,
    reject_legacy_inference_environment,
    resolve_inference_settings,
)
from app.inference.contracts import InferenceTarget, ProviderKind, TargetCriticality
from app.inference.errors import InferenceError, InferenceErrorCategory
from app.inference.health import InferenceHealth
from app.inference.observer import InferenceObservation
from app.inference.registry import ProviderRegistry, default_provider_registry
from app.inference.router import InferenceRouter
from app.llm.ollama_config import (
    DEFAULT_OLLAMA_BASE_URL,
    OLLAMA_BASE_URL_ENV,
)


logger = logging.getLogger(__name__)
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_CODEX_HOME_ENV = "OPENAI_CODEX_HOME"
OPENAI_CODEX_EXECUTABLE_ENV = "OPENAI_CODEX_EXECUTABLE"
FORBIDDEN_OPENAI_ENDPOINT_ENV_KEYS = frozenset(
    {"OPENAI_API_BASE", "OPENAI_API_ENDPOINT", "OPENAI_BASE_URL"}
)


@dataclass(frozen=True)
class InferenceRuntime:
    settings: InferenceSettings
    registry: ProviderRegistry
    router: InferenceRouter
    health: InferenceHealth
    ollama_adapter: OllamaAdapter
    openai_api_adapter: OpenAIAPIAdapter | None = None
    openai_codex_adapter: OpenAICodexAdapter | None = None

    def probe_startup(self) -> None:
        required_failure: InferenceErrorCategory | None = None
        for target, resolved in self.settings.targets.items():
            adapter = self.registry.adapter(resolved.reference.provider_id)
            try:
                adapter.probe(
                    resolved.reference.model_id,
                    timeout_seconds=min(resolved.timeout_seconds, 10.0),
                )
            except Exception as error:
                category = (
                    error.category
                    if isinstance(error, InferenceError)
                    else InferenceErrorCategory.PROVIDER_ERROR
                )
                self.health.record_failure(target, category)
                logger.warning(
                    "Inference startup probe failed target=%s provider=%s model=%s error_category=%s",
                    target.value,
                    resolved.reference.provider_id,
                    resolved.reference.model_id,
                    category.value,
                )
                if resolved.definition.criticality is TargetCriticality.REQUIRED:
                    required_failure = required_failure or category
            else:
                self.health.record_success(target)
                logger.info(
                    "Inference startup probe succeeded target=%s provider=%s model=%s",
                    target.value,
                    resolved.reference.provider_id,
                    resolved.reference.model_id,
                )
        if required_failure is not None:
            raise InferenceError(required_failure, retryable=False)

    def close(self) -> None:
        if self.openai_codex_adapter is not None:
            self.openai_codex_adapter.close()
        if self.openai_api_adapter is not None:
            self.openai_api_adapter.close()
        self.ollama_adapter.close()


def create_inference_runtime(environment: Mapping[str, str]) -> InferenceRuntime:
    registry = default_provider_registry()
    reject_legacy_inference_environment(environment)
    settings = resolve_inference_settings(environment, registry)
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
    health = InferenceHealth(settings)

    def observe(observation: InferenceObservation) -> None:
        if observation.success and observation.external_request_count > 0:
            health.record_success(observation.target)
        elif observation.error_category is not None:
            health.record_failure(observation.target, observation.error_category)
        estimate = observation.token_estimate
        usage = observation.usage
        logger.info(
            json.dumps(
                {
                    "event": "inference_request",
                    "request_id": observation.request_id,
                    "caller": observation.caller.value,
                    "target": observation.target.value,
                    "capability": observation.capability.value,
                    "provider": observation.provider_id,
                    "model": observation.model_id,
                    "auth_kind": observation.auth_kind,
                    "latency_ms": round(observation.latency_ms, 3),
                    "external_request_count": observation.external_request_count,
                    "token_estimate": None
                    if estimate is None
                    else {
                        "count": estimate.count,
                        "accuracy": estimate.accuracy.value,
                        "method": estimate.method,
                    },
                    "usage": None
                    if usage is None
                    else {
                        "input": usage.input_tokens,
                        "output": usage.output_tokens,
                        "total": usage.total_tokens,
                        "provider_reported": usage.provider_reported,
                    },
                    "success": observation.success,
                    "error_category": None
                    if observation.error_category is None
                    else observation.error_category.value,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    router = InferenceRouter(
        settings=settings,
        registry=registry,
        observer=observe,
    )
    return InferenceRuntime(
        settings,
        registry,
        router,
        health,
        ollama_adapter,
        openai_api_adapter,
        openai_codex_adapter,
    )


def target_model_id(settings: InferenceSettings, target: InferenceTarget) -> str:
    return settings.target(target).reference.model_id

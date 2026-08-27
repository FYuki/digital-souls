from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from adapters.backend import BackendAdapter
from adapters.base import CommandRunner, OperationContext, ReadinessOperations, ServiceOperations
from adapters.livekit import LiveKitExternalOperations
from adapters.frontend import FrontendAdapter
from adapters.ollama import OllamaAdapter
from adapters.voicevox import VoicevoxAdapter
from environment_constants import DEPENDENCY_NAMES, OPTIONAL_DEPENDENCY_NAMES
from app.model_settings import OLLAMA_MODEL_NAME, WHISPER_MODEL_NAME
from app.runtime_paths import RuntimePaths


@dataclass(frozen=True)
class ServiceRegistration:
    name: str
    adapter: ServiceOperations | None
    contained_by: str | None
    readiness_adapter: ReadinessOperations | None = None


@dataclass(frozen=True)
class ServiceRegistry:
    services: Mapping[str, ServiceRegistration]
    prepare_order: tuple[str, ...]
    start_order: tuple[str, ...]
    available_prepare_order: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "services", MappingProxyType(dict(self.services)))


@dataclass(frozen=True)
class RuntimeServices:
    prepare_order: tuple[str, ...]
    start_order: tuple[str, ...]
    available_prepare_order: tuple[str, ...]


def create_service_registry(
    root_dir: Path,
    runtime_paths: RuntimePaths,
    runner: CommandRunner | None = None,
    *,
    effective_profile: str = "dev",
    ollama_model_name: str = OLLAMA_MODEL_NAME,
    ollama_classifier_model_name: str | None = None,
    whisper_model_name: str = WHISPER_MODEL_NAME,
) -> ServiceRegistry:
    services = {
        "frontend": ServiceRegistration(
            "frontend",
            FrontendAdapter(
                root_dir, runner, effective_profile=effective_profile
            ),
            None,
        ),
        "backend": ServiceRegistration(
            "backend",
            BackendAdapter(
                root_dir,
                runtime_paths,
                runner,
                whisper_model_name=whisper_model_name,
            ),
            None,
        ),
        "ollama": ServiceRegistration(
            "ollama",
            OllamaAdapter(
                root_dir,
                runner,
                model_name=ollama_model_name,
                classifier_model_name=ollama_classifier_model_name,
            ),
            None,
        ),
        "voicevox": ServiceRegistration(
            "voicevox", VoicevoxAdapter(root_dir, runner), None
        ),
        "whisper": ServiceRegistration("whisper", None, "backend"),
        "chroma": ServiceRegistration("chroma", None, "backend"),
        "livekit": ServiceRegistration(
            "livekit", None, None, LiveKitExternalOperations()
        ),
    }
    return ServiceRegistry(
        services=services,
        prepare_order=("backend", "frontend"),
        start_order=("ollama", "voicevox", "backend", "frontend"),
        available_prepare_order=("ollama",),
    )


def resolve_runtime_services(
    profile: Mapping[str, object], registry: ServiceRegistry
) -> RuntimeServices:
    dependencies = profile.get("dependencies")
    if (
        not isinstance(dependencies, dict)
        or not set(DEPENDENCY_NAMES) <= set(dependencies)
        or not set(dependencies) <= set(DEPENDENCY_NAMES + OPTIONAL_DEPENDENCY_NAMES)
    ):
        raise ValueError("resolved profile must define all dependencies")
    managed = {
        name
        for name, dependency in dependencies.items()
        if isinstance(dependency, dict) and dependency.get("source") == "managed"
    }
    return RuntimeServices(
        prepare_order=tuple(name for name in registry.prepare_order if name in managed),
        start_order=tuple(name for name in registry.start_order if name in managed),
        available_prepare_order=tuple(
            name for name in registry.available_prepare_order if name in managed
        ),
    )


def operation_context_for(
    service: str,
    dependencies: Mapping[str, object],
    registry: ServiceRegistry,
) -> OperationContext:
    contained = {
        name
        for name, registration in registry.services.items()
        if registration.contained_by == service
    }

    def enabled(name: str) -> bool:
        dependency = dependencies.get(name)
        if not isinstance(dependency, dict):
            raise ValueError(f"invalid contained dependency: {name}")
        return name in contained and dependency.get("mode") != "disabled"

    return OperationContext(
        whisper_enabled=enabled("whisper"),
        chroma_enabled=enabled("chroma"),
    )


def require_service_operations(
    registry: ServiceRegistry, service: str
) -> ServiceOperations:
    registration = registry.services.get(service)
    if registration is None or registration.adapter is None:
        raise ValueError(f"service has no lifecycle adapter: {service}")
    return registration.adapter


def require_readiness_operations(
    registry: ServiceRegistry, service: str
) -> ReadinessOperations:
    registration = registry.services.get(service)
    if registration is None:
        raise ValueError(f"service is not registered: {service}")
    operations = registration.readiness_adapter or registration.adapter
    if operations is None:
        raise ValueError(f"service has no readiness adapter: {service}")
    return operations

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from adapters.backend import BackendAdapter
from adapters.base import CommandRunner, OperationContext, ServiceOperations
from adapters.frontend import FrontendAdapter
from adapters.ollama import OllamaAdapter
from adapters.voicevox import VoicevoxAdapter
from environment_constants import DEPENDENCY_NAMES


@dataclass(frozen=True)
class ServiceRegistration:
    name: str
    adapter: ServiceOperations | None
    contained_by: str | None


@dataclass(frozen=True)
class ServiceRegistry:
    services: Mapping[str, ServiceRegistration]
    prepare_order: tuple[str, ...]
    start_order: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "services", MappingProxyType(dict(self.services)))


@dataclass(frozen=True)
class RuntimeServices:
    prepare_order: tuple[str, ...]
    start_order: tuple[str, ...]


def create_service_registry(
    root_dir: Path, runner: CommandRunner | None = None
) -> ServiceRegistry:
    services = {
        "frontend": ServiceRegistration(
            "frontend", FrontendAdapter(root_dir, runner), None
        ),
        "backend": ServiceRegistration("backend", BackendAdapter(root_dir, runner), None),
        "ollama": ServiceRegistration("ollama", OllamaAdapter(root_dir, runner), None),
        "voicevox": ServiceRegistration(
            "voicevox", VoicevoxAdapter(root_dir, runner), None
        ),
        "whisper": ServiceRegistration("whisper", None, "backend"),
        "chroma": ServiceRegistration("chroma", None, "backend"),
    }
    return ServiceRegistry(
        services=services,
        prepare_order=("backend", "frontend"),
        start_order=("ollama", "voicevox", "backend", "frontend"),
    )


def resolve_runtime_services(
    profile: Mapping[str, object], registry: ServiceRegistry
) -> RuntimeServices:
    dependencies = profile.get("dependencies")
    if not isinstance(dependencies, dict) or set(dependencies) != set(DEPENDENCY_NAMES):
        raise ValueError("resolved profile must define all dependencies")
    managed = {
        name
        for name, dependency in dependencies.items()
        if isinstance(dependency, dict) and dependency.get("source") == "managed"
    }
    return RuntimeServices(
        prepare_order=tuple(name for name in registry.prepare_order if name in managed),
        start_order=tuple(name for name in registry.start_order if name in managed),
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

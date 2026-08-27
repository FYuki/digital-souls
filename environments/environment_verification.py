from __future__ import annotations

from typing import Mapping, Protocol

from adapters.base import ReadinessValidationResult
from orchestrator import classify_verification
from service_registry import (
    ServiceRegistry,
    operation_context_for,
    require_readiness_operations,
    require_service_operations,
    resolve_runtime_services,
)


class EnvironmentVerificationError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class ReadinessRecorder(Protocol):
    def __call__(self, service: str, readiness: Mapping[str, object]) -> None: ...


def verification_checks(
    profile: Mapping[str, object],
    registry: ServiceRegistry,
    *,
    request_timeout_seconds: float,
) -> dict[str, dict[str, object]]:
    dependencies = profile["dependencies"]
    if not isinstance(dependencies, dict):
        raise ValueError("resolved dependencies are required")
    preparable_services = set(
        resolve_runtime_services(profile, registry).available_prepare_order
    )
    services: dict[str, dict[str, object]] = {}
    for name in dependencies:
        dependency = dependencies[name]
        if not isinstance(dependency, dict):
            raise ValueError(f"invalid dependency: {name}")
        source = dependency.get("source")
        mode = dependency.get("mode")
        if mode == "disabled" or source in {"browser", "in_process"}:
            services[name] = {"classification": str(source or "disabled"), "checks": []}
            continue
        readiness_operations = require_readiness_operations(registry, name)
        operations = (
            None if source == "external" else require_service_operations(registry, name)
        )
        verification = None
        if source != "external":
            assert operations is not None
            verification = operations.verify(
                dependency, operation_context_for(name, dependencies, registry)
            )
        observation = readiness_operations.probe(
            dependency, timeout_seconds=request_timeout_seconds
        )
        validation = None
        if observation.result == "ready":
            validation = readiness_operations.validate_readiness(dependency)
        classification = (
            "preparation_required"
            if validation is not None and validation.classification == "preparation"
            else "readiness"
            if validation is not None and validation.classification == "readiness"
            else classify_verification(
                source=str(source),
                prerequisites_ready=(
                    True if verification is None else verification.prerequisites_ready
                ),
                probe_result=observation.result,
            )
        )
        checks = [
            {
                "name": check.name,
                "classification": check.classification,
                "message": check.message,
                "canPrepare": check.can_prepare,
            }
            for check in (() if verification is None else verification.checks)
        ]
        if validation is not None and validation.classification != "ready":
            checks.append(
                {
                    "name": f"{name}-readiness-validation",
                    "classification": classification,
                    "message": validation.message,
                    "canPrepare": name in preparable_services,
                }
            )
        services[name] = {
            "classification": classification,
            "checks": checks,
            "readiness": observation.to_report(),
        }
    return services


def record_and_validate_verification(
    results: Mapping[str, Mapping[str, object]],
    record_readiness: ReadinessRecorder,
) -> None:
    for name, result in results.items():
        readiness = result.get("readiness")
        if readiness is not None:
            if not isinstance(readiness, dict):
                raise ValueError(f"invalid verification readiness: {name}")
            record_readiness(name, readiness)
    validate_verification_results(results)


def validate_verification_results(
    results: Mapping[str, Mapping[str, object]],
) -> None:
    for name, result in results.items():
        classification = result["classification"]
        if classification == "preparation_required":
            _require_preparable_service(name, result)
        if classification == "readiness":
            raise EnvironmentVerificationError(
                "readiness", f"external service is not ready: {name}"
            )


def _require_preparable_service(
    name: str, result: Mapping[str, object]
) -> None:
    checks = result["checks"]
    if not isinstance(checks, list):
        raise ValueError(f"invalid verification checks: {name}")
    blocking = [
        check
        for check in checks
        if isinstance(check, dict)
        and check.get("classification") == "preparation_required"
        and check.get("canPrepare") is not True
    ]
    if blocking:
        recovery_messages = [
            check["message"]
            for check in blocking
            if isinstance(check.get("message"), str) and check["message"]
        ]
        details = (
            f"; {'; '.join(recovery_messages)}" if recovery_messages else ""
        )
        raise EnvironmentVerificationError(
            "preparation", f"service preparation is required: {name}{details}"
        )


def require_service_readiness(validation: ReadinessValidationResult) -> None:
    if validation.classification == "ready":
        return
    category = (
        "preparation"
        if validation.classification == "preparation"
        else "readiness"
    )
    if validation.message is None:
        raise ValueError("invalid service readiness validation")
    raise EnvironmentVerificationError(category, validation.message)

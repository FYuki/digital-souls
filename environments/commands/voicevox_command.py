from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from adapters.base import (
    AdapterOperationError,
    OperationContext,
    ServiceOperations,
    ServiceStartResult,
    StopResult,
)
from http_readiness import ReadinessResult, wait_for_http
from environment_signals import (
    defer_interrupt_signals,
    install_interrupt_handlers,
    restore_interrupt_handlers,
)
from environment_timing import EnvironmentTiming
from profile_resolution import resolve_profile
from profile_types import ProfileError
from service_registry import (
    ServiceRegistry,
    create_service_registry,
    operation_context_for,
    require_service_operations,
)


def start_voicevox(
    root_dir: Path,
    default_profile: str | None,
    *,
    registry: ServiceRegistry | None = None,
    timing: EnvironmentTiming | None = None,
) -> int:
    resolved_registry = (
        registry if registry is not None else create_service_registry(root_dir)
    )
    resolved_timing = timing if timing is not None else EnvironmentTiming()
    ownership: ServiceStartResult | None = None
    _was_interrupted, previous_handlers = install_interrupt_handlers()
    try:
        profile = resolve_profile(dict(os.environ), default_profile)
        dependencies = profile["dependencies"]
        if not isinstance(dependencies, dict):
            raise ValueError("resolved dependencies are required")
        dependency = dependencies["voicevox"]
        if not isinstance(dependency, dict) or dependency.get("source") != "managed":
            raise ProfileError("start-voicevox requires managed VOICEVOX")
        operations = require_service_operations(resolved_registry, "voicevox")
        context = operation_context_for("voicevox", dependencies, resolved_registry)
        _verify_voicevox(operations, dependency, context)
        operations.prepare(dependency, context)
        with defer_interrupt_signals():
            ownership = operations.start(dependency, profile["derivedEnvironment"])
        _wait_until_voicevox_ready(operations, dependency, resolved_timing)
    except BaseException as error:
        ownership_from_error = (
            error.ownership
            if ownership is None and isinstance(error, AdapterOperationError)
            else None
        )
        cleanup_ownership = (
            ownership if ownership is not None else ownership_from_error
        )
        cleanup = (
            None
            if cleanup_ownership is None
            else _stop_owned_voicevox(operations, cleanup_ownership)
        )
        if (
            cleanup is not None
            and cleanup.result == "failed"
            and ownership_from_error is None
        ):
            raise AdapterOperationError(
                "readiness", str(error), cleanup_failure=cleanup
            ) from error
        raise
    finally:
        restore_interrupt_handlers(previous_handlers)
    return 0


def _verify_voicevox(
    operations: ServiceOperations,
    dependency: Mapping[str, object],
    context: OperationContext,
) -> None:
    verification = operations.verify(dependency, context)
    if verification.prerequisites_ready:
        return
    blocking = next(
        check.message
        for check in verification.checks
        if check.classification == "preparation_required"
    )
    raise RuntimeError(blocking)


def _wait_until_voicevox_ready(
    operations: ServiceOperations,
    dependency: Mapping[str, object],
    timing: EnvironmentTiming,
) -> None:
    def probe_service(url: str, *, timeout_seconds: float) -> ReadinessResult:
        return operations.probe(dependency, timeout_seconds)

    readiness = wait_for_http(
        str(dependency["readinessUrl"]),
        max_attempts=timing.readiness_attempts,
        interval_seconds=timing.readiness_interval_seconds,
        request_timeout_seconds=timing.request_timeout_seconds,
        probe=probe_service,
    )
    if readiness.result != "ready":
        raise RuntimeError("VOICEVOX did not become ready")
    validation = operations.validate_readiness(dependency)
    if validation.classification != "ready":
        raise RuntimeError(str(validation.message))


def _stop_owned_voicevox(
    operations: ServiceOperations, ownership: ServiceStartResult
) -> StopResult | None:
    if not ownership.owned:
        return None
    try:
        return operations.stop(
            {
                "state": ownership.state,
                "owned": ownership.owned,
                "containerIdentity": ownership.container_identity,
            },
            5.0,
        )
    except Exception as error:
        return StopResult("failed", str(error))

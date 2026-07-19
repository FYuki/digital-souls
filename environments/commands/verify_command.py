from __future__ import annotations

import json
import os
from pathlib import Path

from environment_verification import verification_checks, validate_verification_results
from environment_timing import EnvironmentTiming
from profile_resolution import resolve_profile
from service_registry import ServiceRegistry, create_service_registry


def verify_environment(
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
    profile = resolve_profile(dict(os.environ), default_profile)
    services = verification_checks(
        profile,
        resolved_registry,
        request_timeout_seconds=resolved_timing.request_timeout_seconds,
    )
    result = {
        "effectiveProfile": profile["effectiveProfile"],
        "services": services,
    }
    print(json.dumps(result, ensure_ascii=False))
    validate_verification_results(services)
    return 0

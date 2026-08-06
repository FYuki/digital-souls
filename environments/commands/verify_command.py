from __future__ import annotations

import json
import os
from pathlib import Path

from environment_verification import verification_checks, validate_verification_results
from environment_timing import EnvironmentTiming
from profile_resolution import resolve_profile
from service_registry import ServiceRegistry, create_service_registry
from app.runtime_data_root import validate_existing_runtime_data_root
from app.runtime_paths import resolve_runtime_paths


def verify_environment(
    root_dir: Path,
    default_profile: str | None,
    *,
    registry: ServiceRegistry | None = None,
    timing: EnvironmentTiming | None = None,
) -> int:
    resolved_timing = timing if timing is not None else EnvironmentTiming()
    runtime_paths = resolve_runtime_paths(os.environ, root_dir)
    validate_existing_runtime_data_root(runtime_paths, root_dir)
    profile = resolve_profile(dict(os.environ), default_profile, runtime_paths)
    derived = profile["derivedEnvironment"]
    if registry is not None:
        resolved_registry = registry
    elif profile["dependencies"]["backend"]["mode"] == "real":
        resolved_registry = create_service_registry(
            root_dir,
            runtime_paths,
            ollama_model_name=derived["OLLAMA_CHAT_MODEL"],
            whisper_model_name=derived["WHISPER_MODEL"],
        )
    else:
        resolved_registry = create_service_registry(root_dir, runtime_paths)
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

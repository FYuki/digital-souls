from __future__ import annotations

import json
import os
from pathlib import Path

from environment_verification import verification_checks, validate_verification_results
from profile_resolution import resolve_profile
from service_registry import create_service_registry


def verify_environment(root_dir: Path, default_profile: str | None) -> int:
    profile = resolve_profile(dict(os.environ), default_profile)
    services = verification_checks(profile, create_service_registry(root_dir))
    result = {
        "effectiveProfile": profile["effectiveProfile"],
        "services": services,
    }
    print(json.dumps(result, ensure_ascii=False))
    validate_verification_results(services)
    return 0

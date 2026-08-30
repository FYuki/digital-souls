#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
ENVIRONMENTS_DIR = ROOT_DIR / "environments"
sys.path.insert(0, str(ENVIRONMENTS_DIR))

from managed_endpoint import resolve_managed_http_origin  # noqa: E402
from profile_validation import load_profile  # noqa: E402


def main() -> None:
    profile = load_profile("dogfood")
    for dependency_name, prefix in (
        ("ollama", "OLLAMA"),
        ("voicevox", "VOICEVOX"),
        ("whisper", "WHISPER"),
        ("livekit", "LIVEKIT"),
    ):
        dependency = profile["dependencies"][dependency_name]
        endpoint = resolve_managed_http_origin(
            dependency["baseUrl"], f"dogfood.dependencies.{dependency_name}.baseUrl"
        )
        print(f"{prefix}_HOST={endpoint.host}")
        print(f"{prefix}_PORT={endpoint.port}")


if __name__ == "__main__":
    main()

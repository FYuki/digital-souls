from __future__ import annotations

import json
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROFILE_ROOT = REPOSITORY_ROOT / "environments" / "profiles"


@pytest.mark.parametrize(
    ("profile_name", "expected_base_url"),
    [
        ("dev", "http://127.0.0.1:7880"),
        ("dogfood", "http://127.0.0.1:17880"),
        ("integration-voice", "http://127.0.0.1:7880"),
    ],
)
def test_profiles_register_livekit_as_an_external_readiness_endpoint(
    profile_name: str,
    expected_base_url: str,
) -> None:
    profile = json.loads(
        (PROFILE_ROOT / f"{profile_name}.json").read_text(encoding="utf-8")
    )
    dependency = profile["dependencies"].get("livekit")

    assert dependency == {
        "mode": "real",
        "source": "external",
        "baseUrl": expected_base_url,
        "readinessPath": "/",
    }

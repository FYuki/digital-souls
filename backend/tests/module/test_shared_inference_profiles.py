from __future__ import annotations

from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest

from tests.environment_test_support import resolved_runtime_paths


ROOT_DIR = Path(__file__).parent.parent.parent.parent
PROFILE_NAMES = ("dev", "integration-text", "integration-voice")
VOICEVOX_PROFILE_NAMES = ("dev", "integration-voice", "dogfood")


def _resolve(profile_name: str, tmp_path: Path):
    from profile_resolution import resolve_profile

    return resolve_profile(
        {"DS_PROFILE": profile_name}, None, resolved_runtime_paths(tmp_path)
    )


@pytest.mark.parametrize(
    ("profile_name", "voicevox_enabled"),
    [
        ("dev", True),
        ("integration-text", False),
        ("integration-voice", True),
    ],
)
def test_should_reuse_inference_services_without_runtime_ownership(
    profile_name: str,
    voicevox_enabled: bool,
    tmp_path: Path,
) -> None:
    from service_registry import create_service_registry, resolve_runtime_services

    report = _resolve(profile_name, tmp_path)
    dependencies = report["dependencies"]
    registry = create_service_registry(ROOT_DIR, resolved_runtime_paths(tmp_path))

    runtime = resolve_runtime_services(report, registry)

    assert dependencies["ollama"]["source"] == "external"
    assert dependencies["voicevox"]["mode"] == (
        "real" if voicevox_enabled else "disabled"
    )
    assert dependencies["voicevox"]["source"] == (
        "external" if voicevox_enabled else None
    )
    assert runtime.prepare_order == ("backend", "frontend")
    assert runtime.start_order == ("backend", "frontend")
    assert runtime.available_prepare_order == ()


@pytest.mark.parametrize("profile_name", PROFILE_NAMES)
def test_should_cleanup_only_application_services_for_shared_inference_profiles(
    profile_name: str,
    tmp_path: Path,
) -> None:
    from orchestrator import cleanup_owned_services

    dependencies = _resolve(profile_name, tmp_path)["dependencies"]
    active_services = {
        name: {
            "source": dependency["source"],
            "state": "started" if dependency["source"] == "managed" else "external",
            "owned": dependency["source"] == "managed",
        }
        for name, dependency in dependencies.items()
        if dependency["mode"] != "disabled" and dependency["source"] != "in_process"
    }
    stopped: list[str] = []

    results = cleanup_owned_services(
        {"startSequence": list(active_services), "services": active_services},
        {
            name: lambda name=name: stopped.append(name) or {"result": "stopped"}
            for name in active_services
        },
    )

    assert set(stopped) == {"frontend", "backend"}
    assert {result["service"] for result in results} == {"frontend", "backend"}


@pytest.mark.parametrize("profile_name", PROFILE_NAMES)
def test_should_keep_dogfood_managed_ports_disjoint_from_development_profiles(
    profile_name: str,
    tmp_path: Path,
) -> None:
    dogfood = _resolve("dogfood", tmp_path)
    development = _resolve(profile_name, tmp_path)

    def owned_ports(report: dict[str, object]) -> set[int]:
        dependencies = cast(dict[str, dict[str, object]], report["dependencies"])
        ready_gate = cast(dict[str, object], report["readyGate"])
        return {
            cast(int, ready_gate["port"]),
            *(
                cast(int, dependency["port"])
                for dependency in dependencies.values()
                if dependency["source"] == "managed" and "port" in dependency
            ),
        }

    assert owned_ports(dogfood).isdisjoint(owned_ports(development))


@pytest.mark.parametrize("dependency_name", ["ollama", "voicevox"])
@pytest.mark.parametrize("profile_name", PROFILE_NAMES)
def test_should_share_inference_endpoint_from_the_existing_profile_contract(
    dependency_name: str,
    profile_name: str,
    tmp_path: Path,
) -> None:
    dogfood_dependency = _resolve("dogfood", tmp_path)["dependencies"][dependency_name]
    development_dependency = _resolve(profile_name, tmp_path)["dependencies"][
        dependency_name
    ]

    if development_dependency["mode"] == "disabled":
        assert development_dependency == {"mode": "disabled", "source": None}
    else:
        assert development_dependency["baseUrl"] == dogfood_dependency["baseUrl"]
        assert (
            development_dependency["readinessPath"]
            == dogfood_dependency["readinessPath"]
        )


@pytest.mark.parametrize("profile_name", VOICEVOX_PROFILE_NAMES)
def test_should_use_an_ip_loopback_host_for_the_shared_voicevox_endpoint(
    profile_name: str,
    tmp_path: Path,
) -> None:
    dependency = _resolve(profile_name, tmp_path)["dependencies"]["voicevox"]

    assert urlsplit(dependency["baseUrl"]).hostname == "127.0.0.1"

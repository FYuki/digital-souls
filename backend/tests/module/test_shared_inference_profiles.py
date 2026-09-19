from __future__ import annotations

from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest

from tests.environment_test_support import resolved_runtime_paths


ROOT_DIR = Path(__file__).parent.parent.parent.parent
PROFILE_NAMES = ("dev", "dev-voice", "integration-text", "integration-voice", "integration-irodori", "integration-irodori-fault")
VOICEVOX_PROFILE_NAMES = ("dev", "dev-voice", "integration-voice", "dogfood")


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


def test_dev_voice_profile_disables_rag_at_runtime(tmp_path: Path) -> None:
    from profile_resolution import resolve_profile
    report = resolve_profile(
        {"DS_PROFILE": "dev-voice", "RAG_ENABLED": "true"}, None, resolved_runtime_paths(tmp_path)
    )
    assert report["dependencies"]["chroma"] == {"mode": "disabled", "source": None}
    assert report["derivedEnvironment"]["RAG_ENABLED"] == "false"
    assert "voice-chat-real" in report["capabilities"]


@pytest.mark.parametrize("profile_name", ["integration-irodori", "integration-irodori-fault"])
def test_irodori_is_external_and_cannot_be_owned_by_development(tmp_path: Path, profile_name: str) -> None:
    from service_registry import create_service_registry, resolve_runtime_services
    from profile_resolution import derive_capabilities
    from profile_validation import _validate_dependency
    from profile_types import ProfileError
    from adapters.compose_service import BACKEND_ENV_PREFIXES

    report = _resolve(profile_name, tmp_path)
    dependency = report["dependencies"]["irodori"]
    assert dependency["source"] == "external"
    assert report["derivedEnvironment"]["IRODORI_BASE_URL"] == "http://127.0.0.1:50024"
    assert dependency["readinessUrl"] == "http://127.0.0.1:50024/health/ready"
    registry = create_service_registry(ROOT_DIR, resolved_runtime_paths(tmp_path))
    runtime = resolve_runtime_services(report, registry)
    assert registry.services["irodori"].adapter is None
    assert "irodori" not in runtime.start_order
    assert "IRODORI_" in BACKEND_ENV_PREFIXES
    report["dependencies"]["voicevox"] = {"mode": "disabled", "source": None}
    assert "voice-chat-real" in derive_capabilities(report["dependencies"])
    with pytest.raises(ProfileError, match="external"):
        _validate_dependency(profile_name, "irodori", {
            "mode": "real", "source": "managed",
            "baseUrl": "http://127.0.0.1:50024", "readinessPath": "/health/ready",
        })


def test_irodori_fault_profile_cannot_target_normal_livekit(tmp_path: Path) -> None:
    from profile_validation import _validate_dependency
    from profile_types import ProfileError

    report = _resolve("integration-irodori-fault", tmp_path)
    assert report["dependencies"]["livekit"]["baseUrl"] == "http://127.0.0.1:19880"
    assert report["dependencies"]["backend"]["baseUrl"] == "http://localhost:18500"
    for profile_name, url in [
        ("integration-irodori", "http://127.0.0.1:19880"),
        ("integration-irodori-fault", "http://127.0.0.1:7880"),
        ("integration-irodori-fault", "http://127.0.0.1:17880"),
    ]:
        with pytest.raises(ProfileError, match="fixed local service"):
            _validate_dependency(profile_name, "livekit", {
                "mode": "real", "source": "external", "baseUrl": url, "readinessPath": "/",
            })


def test_measurement_formation_setting_is_preserved_in_report_and_backend_container(tmp_path):
    from profile_resolution import resolve_profile
    from adapters.backend import BackendAdapter
    from tests.environment_test_support import RecordingRunner
    key = "VOICE_MEASUREMENT_DISABLE_MEMORY_FORMATION"
    paths = resolved_runtime_paths(tmp_path)
    report = resolve_profile({"DS_PROFILE": "integration-voice", key: "true"}, None, paths)
    from profile_report import validate_resolved_report
    assert validate_resolved_report(report) == report
    assert report["derivedEnvironment"][key] == "true"
    adapter = BackendAdapter(tmp_path, paths, RecordingRunner())
    values = adapter._write_compose_environment(
        report["dependencies"]["backend"], report["derivedEnvironment"],
        host="127.0.0.1", port=18500,
    )
    # Composeが読むbackend env fileへ明示キーが渡ることを確認する。
    from dotenv import dotenv_values
    assert values[key] == "true"
    assert dotenv_values(values["DS_CONTAINER_ENV_FILE"])[key] == "true"


@pytest.mark.parametrize("value", ["invalid", "1", True, None])
def test_profile_rejects_invalid_measurement_policy_values(tmp_path, value):
    from profile_resolution import resolve_profile
    from profile_report import validate_resolved_report
    from profile_types import ProfileError
    key = "VOICE_MEASUREMENT_DISABLE_MEMORY_FORMATION"
    report = _resolve("integration-voice", tmp_path)
    report["derivedEnvironment"][key] = value
    with pytest.raises(ProfileError, match="true or false"):
        validate_resolved_report(report)
    with pytest.raises(ProfileError, match="true or false"):
        resolve_profile({"DS_PROFILE": "integration-voice", key: value}, None, resolved_runtime_paths(tmp_path))


def test_candidate_ollama_resolves_dedicated_endpoint_and_preserves_shared_profile(tmp_path: Path):
    from profile_resolution import resolve_profile
    candidate = resolve_profile(
        {"DS_PROFILE": "integration-irodori-ollama-candidate",
         "OLLAMA_BASE_URL": "http://localhost:11434"},
        None, resolved_runtime_paths(tmp_path),
    )
    shared = _resolve("integration-irodori", tmp_path)
    assert candidate["derivedEnvironment"]["OLLAMA_BASE_URL"] == "http://127.0.0.1:11534"
    assert shared["derivedEnvironment"]["OLLAMA_BASE_URL"] == "http://localhost:11434"
    for name, dependency in shared["dependencies"].items():
        if name != "ollama":
            assert candidate["dependencies"][name] == dependency
    assert candidate["dependencies"]["ollama"]["source"] == "external"
    assert candidate["dependencies"]["ollama"]["readinessUrl"] == "http://127.0.0.1:11534/api/tags"


def test_cuda_graph_candidate_overrides_stale_shared_endpoints(tmp_path: Path):
    from profile_resolution import resolve_profile

    candidate = resolve_profile(
        {"DS_PROFILE": "integration-irodori-cuda-graph",
         "OLLAMA_BASE_URL": "http://localhost:11434",
         "IRODORI_BASE_URL": "http://127.0.0.1:50024"},
        None, resolved_runtime_paths(tmp_path),
    )
    shared = _resolve("integration-irodori", tmp_path)
    assert candidate["derivedEnvironment"]["OLLAMA_BASE_URL"] == "http://127.0.0.1:11534"
    assert candidate["derivedEnvironment"]["IRODORI_BASE_URL"] == "http://127.0.0.1:50026"
    assert shared["derivedEnvironment"]["IRODORI_BASE_URL"] == "http://127.0.0.1:50024"
    for name, dependency in shared["dependencies"].items():
        if name not in {"ollama", "irodori"}:
            assert candidate["dependencies"][name] == dependency
    assert candidate["dependencies"]["irodori"]["source"] == "external"
    assert candidate["dependencies"]["irodori"]["readinessUrl"] == "http://127.0.0.1:50026/health/ready"

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from tests.environment_test_support import resolved_runtime_paths


ROOT_DIR = Path(__file__).parent.parent.parent.parent


def _resolve(profile_name: str, tmp_path: Path):
    from profile_resolution import resolve_profile

    return resolve_profile(
        {"DS_PROFILE": profile_name}, None, resolved_runtime_paths(tmp_path)
    )


@pytest.mark.parametrize(
    ("profile_name", "frontend_port", "backend_port", "ready_gate_port"),
    [
        ("dev", 5173, 8000, 4174),
        ("integration-text", 5173, 8000, 4174),
        ("integration-voice", 5173, 8000, 4174),
        ("dogfood", 15173, 18000, 14174),
    ],
)
def test_should_resolve_profile_endpoint_ports_without_cross_environment_defaults(
    profile_name: str,
    frontend_port: int,
    backend_port: int,
    ready_gate_port: int,
    tmp_path: Path,
) -> None:
    report = _resolve(profile_name, tmp_path)

    assert report["dependencies"]["frontend"]["port"] == frontend_port
    assert report["dependencies"]["backend"]["port"] == backend_port
    assert report["readyGate"]["port"] == ready_gate_port


def test_should_keep_resolved_base_url_host_and_port_correlated(tmp_path: Path) -> None:
    from profile_report import validate_resolved_report

    report = _resolve("dogfood", tmp_path)

    validated = validate_resolved_report(report)

    assert validated["dependencies"]["frontend"]["baseUrl"] == (
        "http://localhost:15173"
    )
    assert validated["dependencies"]["frontend"]["host"] == "localhost"
    assert validated["dependencies"]["frontend"]["port"] == 15173
    assert validated["dependencies"]["backend"]["host"] == "localhost"
    assert validated["dependencies"]["backend"]["port"] == 18000
    assert validated["readyGate"] == {
        "baseUrl": "http://127.0.0.1:14174",
        "host": "127.0.0.1",
        "port": 14174,
    }


def test_should_reject_resolved_report_with_endpoint_fields_from_another_profile(
    tmp_path: Path,
) -> None:
    from copy import deepcopy

    from profile_report import validate_resolved_report
    from profile_types import ProfileError

    report = deepcopy(_resolve("dogfood", tmp_path))
    report["dependencies"]["frontend"]["port"] = 5173

    with pytest.raises(ProfileError, match=r"frontend.*port"):
        validate_resolved_report(report)


@pytest.mark.parametrize(
    ("endpoint_path", "base_url", "host", "port"),
    [
        ("readyGate", "http://127.1.2.3:1", "127.1.2.3", 1),
        ("frontend", "http://[::1]:65535", "::1", 65535),
        ("backend", "http://127.255.255.255:18000", "127.255.255.255", 18000),
    ],
)
def test_should_validate_each_resolved_managed_endpoint_from_its_base_url(
    tmp_path: Path,
    endpoint_path: str,
    base_url: str,
    host: str,
    port: int,
) -> None:
    from profile_report import validate_resolved_report

    report = deepcopy(_resolve("dogfood", tmp_path))
    endpoint = (
        report["readyGate"]
        if endpoint_path == "readyGate"
        else report["dependencies"][endpoint_path]
    )
    endpoint["baseUrl"] = base_url
    endpoint["host"] = host
    endpoint["port"] = port
    if endpoint_path != "readyGate":
        endpoint["readinessUrl"] = (
            base_url.rstrip("/") + endpoint["readinessPath"]
        )
    if endpoint_path == "backend":
        report["derivedEnvironment"]["DS_BACKEND_ORIGIN"] = base_url

    validated = validate_resolved_report(report)

    validated_endpoint = (
        validated["readyGate"]
        if endpoint_path == "readyGate"
        else validated["dependencies"][endpoint_path]
    )
    assert validated_endpoint["host"] == host
    assert validated_endpoint["port"] == port


def test_should_enable_dogfood_rag_without_external_chroma_process(
    tmp_path: Path,
) -> None:
    from service_registry import (
        create_service_registry,
        operation_context_for,
        resolve_runtime_services,
    )

    report = _resolve("dogfood", tmp_path)
    dependencies = report["dependencies"]
    registry = create_service_registry(ROOT_DIR, resolved_runtime_paths(tmp_path))
    runtime = resolve_runtime_services(report, registry)
    backend_context = operation_context_for("backend", dependencies, registry)

    assert dependencies["ollama"]["source"] == "external"
    assert dependencies["voicevox"]["source"] == "external"
    assert dependencies["chroma"] == {"mode": "real", "source": "in_process"}
    assert report["derivedEnvironment"]["RAG_ENABLED"] == "true"
    assert runtime.start_order == ("backend", "frontend")
    assert "chroma" not in runtime.prepare_order
    assert backend_context.chroma_enabled is True


def test_should_accept_dogfood_asset_with_profile_schema_and_central_validator() -> None:
    from profile_validation import validate_profile

    profile = json.loads(
        (ROOT_DIR / "environments" / "profiles" / "dogfood.json").read_text(
            encoding="utf-8"
        )
    )
    schema = json.loads(
        (ROOT_DIR / "environments" / "schemas" / "profile-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    validator.validate(profile)
    validated = validate_profile(profile, "dogfood")

    assert validated["name"] == "dogfood"


@pytest.mark.parametrize(
    "base_url",
    ["http://192.168.1.20:18000", "http://localhost"],
)
def test_should_reject_invalid_managed_endpoint_in_schema_and_central_validator(
    base_url: str,
) -> None:
    from profile_types import ProfileError
    from profile_validation import validate_profile

    profile = json.loads(
        (ROOT_DIR / "environments" / "profiles" / "dogfood.json").read_text(
            encoding="utf-8"
        )
    )
    candidate = deepcopy(profile)
    candidate["dependencies"]["backend"]["baseUrl"] = base_url
    schema = json.loads(
        (ROOT_DIR / "environments" / "schemas" / "profile-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    assert list(validator.iter_errors(candidate))
    with pytest.raises(ProfileError, match=r"backend\.baseUrl"):
        validate_profile(candidate, "dogfood")

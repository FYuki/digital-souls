import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker


ROOT_DIR = Path(__file__).parent.parent.parent.parent
ENVIRONMENTS_DIR = ROOT_DIR / "environments"
DEPENDENCY_NAMES = ["frontend", "backend", "ollama", "voicevox", "whisper", "chroma"]


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dependencies(profile: dict[str, object]) -> dict[str, dict[str, object]]:
    return cast(dict[str, dict[str, object]], profile["dependencies"])


@pytest.fixture
def profile_validator() -> Draft202012Validator:
    schema = _read_json(ENVIRONMENTS_DIR / "schemas" / "profile-v1.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.fixture
def profile_module() -> ModuleType:
    module_path = ENVIRONMENTS_DIR / "profile_validation.py"
    sys.path.insert(0, str(ENVIRONMENTS_DIR))
    try:
        spec = importlib.util.spec_from_file_location("profile_validation", module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(ENVIRONMENTS_DIR))
    return module


@pytest.mark.parametrize(
    "profile_name",
    ["dev", "test-mocked", "integration-text", "integration-voice"],
)
def test_should_accept_initial_profiles_with_draft_2020_12_validator(
    profile_validator: Draft202012Validator,
    profile_name: str,
):
    profile = _read_json(ENVIRONMENTS_DIR / "profiles" / f"{profile_name}.json")

    profile_validator.validate(profile)


@pytest.mark.parametrize(
    ("dependency_name", "invalid_dependency"),
    [
        ("ollama", {"mode": "mock", "source": "browser"}),
        ("whisper", {"mode": "real", "source": "managed"}),
        ("backend", {"mode": "disabled", "source": "managed"}),
        ("backend", {"mode": "mock", "source": "browser", "baseUrl": "http://localhost:8000"}),
        ("ollama", {"mode": "disabled", "source": None, "readinessPath": "/api/tags"}),
    ],
)
def test_should_reject_invalid_dependency_contracts_with_draft_2020_12_validator(
    profile_validator: Draft202012Validator,
    dependency_name: str,
    invalid_dependency: dict[str, object],
):
    profile = _read_json(ENVIRONMENTS_DIR / "profiles" / "integration-voice.json")
    invalid_profile = deepcopy(profile)
    _dependencies(invalid_profile)[dependency_name] = invalid_dependency

    assert list(profile_validator.iter_errors(invalid_profile))


@pytest.mark.parametrize(
    ("base_url", "expected_valid"),
    [
        ("http://localhost:8000", True),
        ("https://example.com/api", True),
        ("http://user:password@localhost:8000", False),
        ("http://localhost:8000?token=secret", False),
        ("http://localhost:8000#secret", False),
        ("http://exa mple.com", False),
        ("http://", False),
        ("http://[bad]", False),
        ("http://[::::]:8000", False),
        ("http://[1::2::3]:8000", False),
        ("http://[12345::1]:8000", False),
        ("http://[::1]:8000", True),
        ("http://[::ffff:192.0.2.1]:8000", True),
        ("http://example.com/%20", True),
        ("http://example.com/%zz", False),
        ("http://%zz", False),
        ("http://localhost:8000\n", False),
    ],
)
def test_should_apply_the_same_http_url_contract_in_schema_and_central_validator(
    profile_validator: Draft202012Validator,
    profile_module: ModuleType,
    base_url: str,
    expected_valid: bool,
):
    profile = _read_json(ENVIRONMENTS_DIR / "profiles" / "integration-voice.json")
    candidate = deepcopy(profile)
    _dependencies(candidate)["backend"]["source"] = "external"
    _dependencies(candidate)["backend"]["baseUrl"] = base_url

    schema_accepts = not list(profile_validator.iter_errors(candidate))
    try:
        profile_module.validate_profile(candidate, "integration-voice")
        central_accepts = True
    except profile_module.ProfileError:
        central_accepts = False

    assert schema_accepts is expected_valid
    assert central_accepts is expected_valid


@pytest.mark.parametrize(
    ("readiness_path", "expected_valid"),
    [
        ("/", True),
        ("/health", True),
        ("/health/", True),
        ("/health/%20", True),
        ("/health;status=ready", True),
        ("//health", False),
        ("/health?token=secret", False),
        ("/health#fragment", False),
        ("/health check", False),
        ("/health\nnext", False),
        ("/health/%zz", False),
    ],
)
def test_should_apply_the_same_rfc3986_readiness_path_contract(
    profile_validator: Draft202012Validator,
    profile_module: ModuleType,
    readiness_path: str,
    expected_valid: bool,
):
    profile = _read_json(ENVIRONMENTS_DIR / "profiles" / "integration-voice.json")
    candidate = deepcopy(profile)
    backend = _dependencies(candidate)["backend"]
    backend["source"] = "external"
    backend["readinessPath"] = readiness_path

    schema_accepts = not list(profile_validator.iter_errors(candidate))
    try:
        profile_module.validate_profile(candidate, "integration-voice")
        central_accepts = True
    except profile_module.ProfileError:
        central_accepts = False

    assert schema_accepts is expected_valid
    assert central_accepts is expected_valid


@pytest.mark.parametrize(
    ("dependency_name", "source", "base_url", "expected_valid"),
    [
        ("frontend", "managed", "http://localhost:5173", True),
        ("frontend", "managed", "http://frontend.example:5173", False),
        ("frontend", "external", "http://frontend.example:5173", True),
        ("backend", "managed", "http://127.0.0.1:8000/", True),
        ("backend", "managed", "http://backend.example:8000", False),
        ("backend", "external", "http://backend.example:8000", True),
        ("ollama", "managed", "http://127.0.0.1:11434/", True),
        ("ollama", "managed", "http://ollama.example:11434", False),
        ("ollama", "external", "http://ollama.example:11434", True),
        ("voicevox", "managed", "http://127.0.0.1:50021/", True),
        ("voicevox", "managed", "http://voicevox.example:50021", False),
        ("voicevox", "external", "http://voicevox.example:50021", True),
    ],
)
def test_should_require_external_source_for_urls_not_managed_by_the_launcher(
    profile_validator: Draft202012Validator,
    profile_module: ModuleType,
    dependency_name: str,
    source: str,
    base_url: str,
    expected_valid: bool,
):
    profile = _read_json(ENVIRONMENTS_DIR / "profiles" / "integration-voice.json")
    candidate = deepcopy(profile)
    dependency = _dependencies(candidate)[dependency_name]
    dependency["source"] = source
    dependency["baseUrl"] = base_url

    schema_accepts = not list(profile_validator.iter_errors(candidate))
    try:
        profile_module.validate_profile(candidate, "integration-voice")
        central_accepts = True
    except profile_module.ProfileError:
        central_accepts = False

    assert schema_accepts is expected_valid
    assert central_accepts is expected_valid


@pytest.mark.parametrize("dependency_name", ["frontend", "backend", "ollama", "voicevox"])
def test_should_require_the_launcher_readiness_path_for_managed_dependencies(
    profile_validator: Draft202012Validator,
    profile_module: ModuleType,
    dependency_name: str,
):
    profile = _read_json(ENVIRONMENTS_DIR / "profiles" / "integration-voice.json")
    candidate = deepcopy(profile)
    _dependencies(candidate)[dependency_name]["readinessPath"] = "/health"

    assert list(profile_validator.iter_errors(candidate))
    with pytest.raises(profile_module.ProfileError, match=rf"{dependency_name}\.readinessPath"):
        profile_module.validate_profile(candidate, "integration-voice")


@pytest.mark.parametrize("dependency_name", ["whisper", "chroma"])
@pytest.mark.parametrize(
    "connection_fields",
    [
        {"baseUrl": "http://localhost:9000"},
        {"readinessPath": "/health"},
        {"baseUrl": "http://localhost:9000", "readinessPath": "/health"},
    ],
)
def test_should_reject_connection_fields_for_in_process_dependencies(
    profile_validator: Draft202012Validator,
    profile_module: ModuleType,
    dependency_name: str,
    connection_fields: dict[str, str],
):
    profile = _read_json(ENVIRONMENTS_DIR / "profiles" / "integration-voice.json")
    candidate = deepcopy(profile)
    _dependencies(candidate)[dependency_name] = {
        "mode": "real",
        "source": "in_process",
        **connection_fields,
    }

    assert list(profile_validator.iter_errors(candidate))
    with pytest.raises(profile_module.ProfileError, match="in_process cannot define connection fields"):
        profile_module.validate_profile(candidate, "integration-voice")


@pytest.mark.parametrize(
    ("schema_version", "expected_valid"),
    [
        (1, True),
        (1.0, True),
        (True, False),
        (2, False),
        ("1", False),
    ],
)
def test_should_apply_the_same_schema_version_contract_in_schema_and_central_validator(
    profile_validator: Draft202012Validator,
    profile_module: ModuleType,
    schema_version: object,
    expected_valid: bool,
):
    profile = _read_json(ENVIRONMENTS_DIR / "profiles" / "integration-voice.json")
    candidate = {**profile, "schemaVersion": schema_version}

    schema_accepts = not list(profile_validator.iter_errors(candidate))
    try:
        profile_module.validate_profile(candidate, "integration-voice")
        central_accepts = True
    except profile_module.ProfileError:
        central_accepts = False

    assert schema_accepts is expected_valid
    assert central_accepts is expected_valid


def test_should_require_disabled_downstream_dependencies_when_backend_is_not_real(
    profile_validator: Draft202012Validator,
):
    profile = _read_json(ENVIRONMENTS_DIR / "profiles" / "test-mocked.json")
    invalid_profile = deepcopy(profile)
    _dependencies(invalid_profile)["ollama"] = {
        "mode": "real",
        "source": "managed",
        "baseUrl": "http://localhost:11434",
        "readinessPath": "/api/tags",
    }

    assert list(profile_validator.iter_errors(invalid_profile))


@pytest.mark.parametrize(
    ("profile_name", "expected_dependencies"),
    [
        (
            "dev",
            {
                "frontend": ("real", "managed"),
                "backend": ("real", "managed"),
                "ollama": ("real", "managed"),
                "voicevox": ("real", "managed"),
                "whisper": ("real", "in_process"),
                "chroma": ("disabled", None),
            },
        ),
        (
            "test-mocked",
            {
                "frontend": ("real", "managed"),
                "backend": ("mock", "browser"),
                "ollama": ("disabled", None),
                "voicevox": ("disabled", None),
                "whisper": ("disabled", None),
                "chroma": ("disabled", None),
            },
        ),
        (
            "integration-text",
            {
                "frontend": ("real", "managed"),
                "backend": ("real", "managed"),
                "ollama": ("real", "managed"),
                "voicevox": ("disabled", None),
                "whisper": ("disabled", None),
                "chroma": ("disabled", None),
            },
        ),
        (
            "integration-voice",
            {
                "frontend": ("real", "managed"),
                "backend": ("real", "managed"),
                "ollama": ("real", "managed"),
                "voicevox": ("real", "managed"),
                "whisper": ("real", "in_process"),
                "chroma": ("disabled", None),
            },
        ),
    ],
)
def test_should_define_initial_profile_dependency_modes(
    profile_name: str,
    expected_dependencies: dict[str, tuple[str, str | None]],
):
    profile = _read_json(ENVIRONMENTS_DIR / "profiles" / f"{profile_name}.json")
    dependencies = _dependencies(profile)

    assert profile["schemaVersion"] == 1
    assert profile["name"] == profile_name
    assert set(dependencies) == set(DEPENDENCY_NAMES)
    assert {
        name: (dependency["mode"], dependency["source"])
        for name, dependency in dependencies.items()
    } == expected_dependencies


@pytest.mark.parametrize(
    ("dependency_name", "base_url", "readiness_path"),
    [
        ("frontend", "http://localhost:5173", "/"),
        ("backend", "http://localhost:8000", "/"),
        ("ollama", "http://localhost:11434", "/api/tags"),
        ("voicevox", "http://localhost:50021", "/version"),
    ],
)
def test_should_use_existing_service_connection_contracts(
    dependency_name: str,
    base_url: str,
    readiness_path: str,
):
    profile = _read_json(ENVIRONMENTS_DIR / "profiles" / "integration-voice.json")
    dependency = _dependencies(profile)[dependency_name]

    assert dependency["baseUrl"] == base_url
    assert dependency["readinessPath"] == readiness_path

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from profile_types import Dependencies, Dependency, DependencyMode, DependencySource, Profile, ProfileError


PROFILE_SCHEMA_VERSION = 1
DEPENDENCY_NAMES = ("frontend", "backend", "ollama", "voicevox", "whisper", "chroma")
DOWNSTREAM_DEPENDENCIES = ("ollama", "voicevox", "whisper", "chroma")
PROFILE_FIELDS = {"schemaVersion", "name", "description", "dependencies"}
DEPENDENCY_FIELDS = {"mode", "source", "baseUrl", "readinessPath"}
MODES = {"real", "mock", "disabled"}
SOURCES = {"managed", "external", "in_process", "browser", None}
PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
HTTP_URL_PATTERN = re.compile(
    r"^https?://(?:\[(?![^\]]*:::)[0-9A-Fa-f.]*:[0-9A-Fa-f:.]+\]|[^/?#\s:@\[\]]+)"
    r"(?::[0-9]+)?(?:/[^?#\s]*)?$"
)
INVALID_PERCENT_ENCODING = re.compile(r"%(?![0-9A-Fa-f]{2})")
READINESS_PATH_PATTERN = re.compile(
    r"^/(?:[A-Za-z0-9._~!$&'()*+,;=:@]|%[0-9A-Fa-f]{2})+"
    r"(?:/(?:[A-Za-z0-9._~!$&'()*+,;=:@]|%[0-9A-Fa-f]{2})*)*$|^/$"
)
MANAGED_HTTP_DEPENDENCY_CONTRACTS = {
    "frontend": ({"http://localhost:5173", "http://localhost:5173/"}, "/"),
    "backend": (
        {
            "http://127.0.0.1:8000",
            "http://127.0.0.1:8000/",
            "http://localhost:8000",
            "http://localhost:8000/",
        },
        "/",
    ),
    "ollama": (
        {
            "http://127.0.0.1:11434",
            "http://127.0.0.1:11434/",
            "http://localhost:11434",
            "http://localhost:11434/",
        },
        "/api/tags",
    ),
    "voicevox": (
        {
            "http://127.0.0.1:50021",
            "http://127.0.0.1:50021/",
            "http://localhost:50021",
            "http://localhost:50021/",
        },
        "/version",
    ),
}


def _require_record(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProfileError(f"{path} must be an object")
    return cast(dict[str, object], value)


def _reject_unknown_fields(record: dict[str, object], allowed: set[str], path: str) -> None:
    unknown = record.keys() - allowed
    if unknown:
        field = sorted(unknown)[0]
        qualified = f"{path}.{field}" if path else field
        raise ProfileError(f"unknown field: {qualified}")


def validate_http_url(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"{path} must be a string")
    if not HTTP_URL_PATTERN.fullmatch(value):
        raise ProfileError(f"{path} must be an http or https URL")
    if INVALID_PERCENT_ENCODING.search(value):
        raise ProfileError(f"{path} must contain valid percent encoding")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ProfileError(f"{path} must be an http or https URL") from error
    if parsed.username is not None or parsed.password is not None:
        raise ProfileError(f"{path} must not contain user information")
    if parsed.query or parsed.fragment or "?" in value or "#" in value:
        raise ProfileError(f"{path} must not contain a query or fragment")
    return value


def validate_http_origin(value: object, path: str) -> str:
    origin = validate_http_url(value, path)
    if urlsplit(origin).path not in {"", "/"}:
        raise ProfileError(f"{path} must not contain a path")
    return origin


def _validate_mode_source(name: str, dependency: Dependency, path: str) -> None:
    mode = dependency["mode"]
    source = dependency["source"]
    if mode == "disabled" and source is not None:
        raise ProfileError(f"{path}.source must be null when mode is disabled")
    if mode == "disabled" and ("baseUrl" in dependency or "readinessPath" in dependency):
        raise ProfileError(f"{path} cannot define connection fields when disabled")
    if mode == "mock" and (name != "backend" or source != "browser"):
        raise ProfileError(f"{path}.mode mock is only valid for backend/browser")
    if mode == "mock" and ("baseUrl" in dependency or "readinessPath" in dependency):
        raise ProfileError(f"{path} mock/browser cannot define connection fields")
    if mode == "real" and name in {"whisper", "chroma"} and source != "in_process":
        raise ProfileError(f"{path}.source must be in_process when mode is real")
    if source == "in_process" and ("baseUrl" in dependency or "readinessPath" in dependency):
        raise ProfileError(f"{path} in_process cannot define connection fields")
    if mode == "real" and name not in {"whisper", "chroma"} and source not in {"managed", "external"}:
        raise ProfileError(f"{path}.source must be managed or external when mode is real")
    if mode == "real" and source in {"managed", "external"}:
        if "baseUrl" not in dependency:
            raise ProfileError(f"{path}.baseUrl is required for real/{source}")
        if "readinessPath" not in dependency:
            raise ProfileError(f"{path}.readinessPath is required for real/{source}")
    if mode == "real" and source == "managed":
        managed_base_urls, managed_readiness_path = MANAGED_HTTP_DEPENDENCY_CONTRACTS[name]
        if dependency["baseUrl"] not in managed_base_urls:
            raise ProfileError(
                f"{path}.baseUrl must identify the local service managed by its launcher"
            )
        if dependency["readinessPath"] != managed_readiness_path:
            raise ProfileError(
                f"{path}.readinessPath must be {managed_readiness_path} when source is managed"
            )


def _validate_dependency(profile_name: str, name: str, raw: object) -> Dependency:
    path = f"{profile_name}.dependencies.{name}"
    record = _require_record(raw, path)
    _reject_unknown_fields(record, DEPENDENCY_FIELDS, path)
    for field in ("mode", "source"):
        if field not in record:
            raise ProfileError(f"{path}.{field} is required")
    mode = record["mode"]
    source = record["source"]
    if not isinstance(mode, str) or mode not in MODES:
        raise ProfileError(f"{path}.mode has unknown value {mode!r}")
    if source is not None and not isinstance(source, str):
        raise ProfileError(f"{path}.source must be a string or null")
    if source not in SOURCES:
        raise ProfileError(f"{path}.source has unknown value {source!r}")
    dependency = cast(Dependency, {
        "mode": cast(DependencyMode, mode),
        "source": cast(DependencySource, source),
        **(
            {"baseUrl": validate_http_url(record["baseUrl"], f"{path}.baseUrl")}
            if "baseUrl" in record
            else {}
        ),
        **_validated_readiness_path(record, path),
    })
    _validate_mode_source(name, dependency, path)
    return dependency


def _validated_readiness_path(record: dict[str, object], path: str) -> dict[str, str]:
    if "readinessPath" not in record:
        return {}
    readiness_path = record["readinessPath"]
    if not isinstance(readiness_path, str) or not READINESS_PATH_PATTERN.fullmatch(readiness_path):
        raise ProfileError(f"{path}.readinessPath must be an RFC 3986 path-absolute value")
    return {"readinessPath": readiness_path}


def validate_profile(raw_profile: object, expected_name: str) -> Profile:
    record = _require_record(raw_profile, expected_name)
    _reject_unknown_fields(record, PROFILE_FIELDS, expected_name)
    for field in PROFILE_FIELDS:
        if field not in record:
            raise ProfileError(f"{expected_name}: {field} is required")
    schema_version = record["schemaVersion"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, (int, float)):
        raise ProfileError(f"{expected_name}: schemaVersion must be {PROFILE_SCHEMA_VERSION}")
    if schema_version != PROFILE_SCHEMA_VERSION:
        raise ProfileError(f"{expected_name}: schemaVersion must be {PROFILE_SCHEMA_VERSION}")
    if record["name"] != expected_name:
        raise ProfileError(f"{expected_name}: name must match profile filename")
    description = record["description"]
    if not isinstance(description, str) or not description:
        raise ProfileError(f"{expected_name}: description must be a non-empty string")
    raw_dependencies = _require_record(record["dependencies"], f"{expected_name}.dependencies")
    _reject_unknown_fields(raw_dependencies, set(DEPENDENCY_NAMES), f"{expected_name}.dependencies")
    for name in DEPENDENCY_NAMES:
        if name not in raw_dependencies:
            raise ProfileError(f"{expected_name}.dependencies.{name} is required")
    dependency_map: dict[str, Dependency] = {
        name: _validate_dependency(expected_name, name, raw_dependencies[name])
        for name in DEPENDENCY_NAMES
    }
    dependencies = cast(Dependencies, dependency_map)
    if dependencies["backend"]["mode"] != "real":
        for name in DOWNSTREAM_DEPENDENCIES:
            dependency = dependency_map[name]
            if dependency["mode"] != "disabled":
                raise ProfileError(
                    f"{expected_name}.dependencies.{name}.mode must be disabled when backend is not real"
                )
    return {
        "schemaVersion": 1,
        "name": expected_name,
        "description": description,
        "dependencies": dependencies,
    }


def load_profile(name: str) -> Profile:
    if not PROFILE_NAME_PATTERN.fullmatch(name):
        raise ProfileError(f"profile name is invalid: {name!r}")
    path = Path(__file__).resolve().parent / "profiles" / f"{name}.json"
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ProfileError(f"profile {name!r} does not exist") from error
    except OSError as error:
        raise ProfileError(f"profile {name!r} cannot be read: {error.strerror or error}") from error
    except json.JSONDecodeError as error:
        raise ProfileError(f"profile {name!r} contains invalid JSON: {error.msg}") from error
    return validate_profile(raw, name)

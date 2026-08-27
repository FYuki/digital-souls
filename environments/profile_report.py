from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import cast

from app.model_settings import MODEL_ENVIRONMENT_KEYS, resolve_model_settings
from app.runtime_paths import (
    CACHE_DIRECTORY,
    CHROMA_DIRECTORY,
    PERSONA_MEMORY_SQLITE_FILENAME,
    RUNTIME_DIRECTORY,
    SQLITE_FILENAME,
    SUPPORTED_ENVIRONMENT_IDS,
)
from profile_constants import PROFILE_ENV
from profile_resolution import derive_capabilities, derive_environment, resolve_dependencies
from profile_types import (
    LegacyBackendReport,
    ProfileError,
    ResolvedDependency,
    ResolvedDependencies,
    ResolvedReport,
)
from profile_validation import (
    DEPENDENCY_FIELDS,
    DEPENDENCY_NAMES,
    REQUIRED_DEPENDENCY_NAMES,
    validate_profile,
)
from managed_endpoint import resolve_managed_http_origin


REPORT_FIELDS = {
    "reportSchemaVersion",
    "generatedAt",
    "requestedProfile",
    "effectiveProfile",
    "selectionSource",
    "profile",
    "readyGate",
    "dependencies",
    "capabilities",
    "derivedEnvironment",
    "runtime",
    "compatibility",
}
RESOLVED_DEPENDENCY_FIELDS = DEPENDENCY_FIELDS | {"readinessUrl", "host", "port"}
SELECTION_SOURCES = {PROFILE_ENV, "default-profile", "legacy-environment"}


def create_legacy_report(report: ResolvedReport) -> LegacyBackendReport:
    source = report["selectionSource"]
    profile = report["effectiveProfile"]
    if source == PROFILE_ENV:
        reason = f"resolved from {PROFILE_ENV}={profile}"
    elif source == "default-profile":
        reason = f"resolved from default-profile={profile}"
    else:
        reason = f"resolved from legacy environment as {profile}"
    return {"mode": report["dependencies"]["backend"]["mode"], "reasons": [reason]}


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ProfileError(f"report does not exist: {path}") from error
    except OSError as error:
        raise ProfileError(f"report cannot be read at {path}: {error.strerror or error}") from error
    except json.JSONDecodeError as error:
        raise ProfileError(f"report contains invalid JSON: {error.msg}") from error


def _require_record(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProfileError(f"{path} must be an object")
    return cast(dict[str, object], value)


def _require_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProfileError(f"{path} must be a non-empty string")
    return value


def _require_string_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProfileError(f"{path} must be an array of strings")
    return cast(list[str], value)


def _reject_unknown_fields(record: dict[str, object], allowed: set[str], path: str) -> None:
    unknown = record.keys() - allowed
    if unknown:
        raise ProfileError(f"unknown field: {path}.{sorted(unknown)[0]}")


def _validate_generated_at(value: object) -> None:
    generated_at = _require_string(value, "generatedAt")
    try:
        parsed = datetime.fromisoformat(generated_at)
    except ValueError as error:
        raise ProfileError("generatedAt must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProfileError("generatedAt must include a timezone")


def _validate_compatibility(value: object) -> None:
    compatibility = _require_record(value, "compatibility")
    _reject_unknown_fields(
        compatibility,
        {"usedEnvironmentVariables", "warnings"},
        "compatibility",
    )
    for field in ("usedEnvironmentVariables", "warnings"):
        if field not in compatibility:
            raise ProfileError(f"compatibility.{field} is required")
        _require_string_list(compatibility[field], f"compatibility.{field}")


def _validate_dependencies(
    value: object, profile_name: str, ready_gate_base_url: str
) -> ResolvedDependencies:
    dependencies = _require_record(value, "dependencies")
    _reject_unknown_fields(dependencies, set(DEPENDENCY_NAMES), "dependencies")
    dependency_records = {
        name: _require_record(dependencies[name], f"dependencies.{name}")
        for name in DEPENDENCY_NAMES
        if name in dependencies
    }
    for name, dependency in dependency_records.items():
        _reject_unknown_fields(dependency, RESOLVED_DEPENDENCY_FIELDS, f"dependencies.{name}")
    for name in REQUIRED_DEPENDENCY_NAMES:
        if name not in dependencies:
            raise ProfileError(f"dependencies.{name} is required")

    unresolved = {
        name: {
            field: field_value
            for field, field_value in dependency.items()
            if field not in {"readinessUrl", "host", "port"}
        }
        for name, dependency in dependency_records.items()
    }
    readiness_urls = {
        name: dependency["readinessUrl"]
        for name, dependency in dependency_records.items()
        if "readinessUrl" in dependency
    }

    profile = validate_profile(
        {
            "schemaVersion": 1,
            "name": profile_name,
            "description": "resolved report",
            "readyGate": {"baseUrl": ready_gate_base_url},
            "dependencies": unresolved,
        },
        profile_name,
    )
    resolved = resolve_dependencies(profile["dependencies"])
    resolved_map = cast(dict[str, ResolvedDependency], resolved)
    for name in dependency_records:
        expected = resolved_map[name].get("readinessUrl")
        actual = readiness_urls.get(name)
        if (expected is None and name in readiness_urls) or actual != expected:
            raise ProfileError(
                f"dependencies.{name}.readinessUrl must match baseUrl and readinessPath"
            )
        expected_host = resolved_map[name].get("host")
        expected_port = resolved_map[name].get("port")
        actual_host = dependency_records[name].get("host")
        actual_port = dependency_records[name].get("port")
        if (expected_host is None and "host" in dependency_records[name]) or actual_host != expected_host:
            raise ProfileError(f"dependencies.{name}.host must match baseUrl")
        if (expected_port is None and "port" in dependency_records[name]) or actual_port != expected_port:
            raise ProfileError(f"dependencies.{name}.port must match baseUrl")
    return resolved


def _validate_ready_gate(value: object) -> dict[str, object]:
    ready_gate = _require_record(value, "readyGate")
    _reject_unknown_fields(ready_gate, {"baseUrl", "host", "port"}, "readyGate")
    if set(ready_gate) != {"baseUrl", "host", "port"}:
        raise ProfileError("readyGate must define baseUrl, host, and port")
    endpoint = resolve_managed_http_origin(ready_gate["baseUrl"], "readyGate.baseUrl")
    if ready_gate["host"] != endpoint.host:
        raise ProfileError("readyGate.host must match baseUrl")
    if ready_gate["port"] != endpoint.port:
        raise ProfileError("readyGate.port must match baseUrl")
    return ready_gate


def validate_resolved_report(raw: object) -> ResolvedReport:
    report = _require_record(raw, "resolved report")
    _reject_unknown_fields(report, REPORT_FIELDS, "resolved report")
    for field in REPORT_FIELDS:
        if field not in report:
            raise ProfileError(f"{field} is required")
    if report["reportSchemaVersion"] != 1 or isinstance(report["reportSchemaVersion"], bool):
        raise ProfileError("reportSchemaVersion must be 1")
    _validate_generated_at(report["generatedAt"])
    requested_profile = _require_string(report["requestedProfile"], "requestedProfile")
    effective_profile = _require_string(report["effectiveProfile"], "effectiveProfile")
    if requested_profile != effective_profile:
        raise ProfileError("requestedProfile must match effectiveProfile")
    selection_source = _require_string(report["selectionSource"], "selectionSource")
    if selection_source not in SELECTION_SOURCES:
        raise ProfileError("selectionSource has an unknown value")

    identity = _require_record(report["profile"], "profile")
    _reject_unknown_fields(identity, {"schemaVersion", "name"}, "profile")
    if identity.get("schemaVersion") != 1 or isinstance(identity.get("schemaVersion"), bool):
        raise ProfileError("profile.schemaVersion must be 1")
    if identity.get("name") != effective_profile:
        raise ProfileError("profile.name must match effectiveProfile")

    ready_gate = _validate_ready_gate(report["readyGate"])
    dependencies = _validate_dependencies(
        report["dependencies"], effective_profile, cast(str, ready_gate["baseUrl"])
    )
    capabilities = _require_string_list(report["capabilities"], "capabilities")
    if capabilities != derive_capabilities(dependencies):
        raise ProfileError("capabilities must match the resolved dependencies")
    derived_environment = _require_record(report["derivedEnvironment"], "derivedEnvironment")
    runtime = _require_record(report["runtime"], "runtime")
    runtime_fields = {
        "environmentId", "dataRoot", "sqlitePath", "personaMemorySqlitePath", "chromaPath",
        "runtimeReportDirectory", "cachePath",
    }
    legacy_runtime_fields = runtime_fields - {"personaMemorySqlitePath"}
    _reject_unknown_fields(runtime, runtime_fields, "runtime")
    if frozenset(runtime) not in {
        frozenset(runtime_fields),
        frozenset(legacy_runtime_fields),
    }:
        raise ProfileError("runtime must define every resolved path")
    for name, value in runtime.items():
        _require_string(value, f"runtime.{name}")
    data_root = Path(cast(str, runtime["dataRoot"]))
    expected_paths = {
        "sqlitePath": data_root / SQLITE_FILENAME,
        "personaMemorySqlitePath": data_root / PERSONA_MEMORY_SQLITE_FILENAME,
        "chromaPath": data_root / CHROMA_DIRECTORY,
        "runtimeReportDirectory": data_root / RUNTIME_DIRECTORY,
        "cachePath": data_root / CACHE_DIRECTORY,
    }
    if not data_root.is_absolute() or data_root.resolve(strict=False) != data_root:
        raise ProfileError("runtime.dataRoot must be a normalized absolute path")
    if runtime["environmentId"] not in SUPPORTED_ENVIRONMENT_IDS:
        raise ProfileError("runtime.environmentId is unsupported")
    if any(
        runtime[name] != str(path)
        for name, path in expected_paths.items()
        if name in runtime
    ):
        raise ProfileError("runtime paths must be derived from runtime.dataRoot")
    if derived_environment.get("DS_ENVIRONMENT_ID") != runtime["environmentId"]:
        raise ProfileError("derivedEnvironment identity must match runtime")
    if derived_environment.get("DS_DATA_DIR") != runtime["dataRoot"]:
        raise ProfileError("derivedEnvironment data root must match runtime")
    model_environment = {
        key: value
        for key, value in derived_environment.items()
        if key in MODEL_ENVIRONMENT_KEYS
        and isinstance(value, str)
    }
    backend_uses_model_settings = dependencies["backend"]["mode"] == "real"
    if backend_uses_model_settings and set(model_environment) != set(
        MODEL_ENVIRONMENT_KEYS
    ):
        raise ProfileError(
            "derivedEnvironment must define every model setting for a real backend"
        )
    try:
        expected_environment = derive_environment(
            dependencies,
            (
                resolve_model_settings(model_environment)
                if backend_uses_model_settings
                else None
            ),
        )
    except ValueError as error:
        raise ProfileError(str(error)) from error
    expected_environment = {
        **expected_environment,
        "DS_ENVIRONMENT_ID": runtime["environmentId"],
        "DS_DATA_DIR": str(data_root),
    }
    if derived_environment != expected_environment:
        raise ProfileError("derivedEnvironment must match the resolved dependencies")
    _validate_compatibility(report["compatibility"])
    return cast(ResolvedReport, report)


def load_resolved_report(path: Path) -> ResolvedReport:
    return validate_resolved_report(_read_json(path))


def read_report_value(path: Path, json_path: str) -> object:
    value: object = load_resolved_report(path)
    for segment in json_path.split("."):
        if not isinstance(value, dict) or segment not in value:
            raise ProfileError(f"report path does not exist: {json_path}")
        value = value[segment]
    if isinstance(value, (dict, list)):
        raise ProfileError(f"report path is not scalar: {json_path}")
    return value

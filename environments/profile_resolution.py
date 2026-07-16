from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, cast

from profile_constants import (
    BACKEND_DERIVED_ORIGIN_ENV,
    BACKEND_ORIGIN_ENV,
    CHAT_BACKEND_ENV,
    LEGACY_REPORT_ENV,
    OLLAMA_BASE_URL_ENV,
    PROFILE_ENV,
    RAG_ENABLED_ENV,
    VOICE_BACKEND_ENV,
    VOICEVOX_BASE_URL_ENV,
)
from profile_types import (
    Capability,
    Compatibility,
    Dependencies,
    Dependency,
    Profile,
    ProfileError,
    ResolvedDependencies,
    ResolvedDependency,
    ResolvedReport,
)
from profile_validation import load_profile, validate_http_origin


REPORT_SCHEMA_VERSION: Literal[1] = 1
LEGACY_SELECTORS = {
    VOICE_BACKEND_ENV: {"mock": "test-mocked", "real": "integration-voice"},
    CHAT_BACKEND_ENV: {"mock": "test-mocked", "real": "integration-text"},
}


def _legacy_profile(env: dict[str, str]) -> tuple[str | None, list[str]]:
    selected_values = [
        (variable, env[variable], mapping)
        for variable, mapping in LEGACY_SELECTORS.items()
        if variable in env
    ]
    for variable, value, mapping in selected_values:
        if value not in mapping:
            raise ProfileError(f'{variable} must be "mock" or "real"; received {value!r}')
    selections = [(variable, value, mapping[value]) for variable, value, mapping in selected_values]
    if not selections:
        return None, []
    values = {(value, profile) for _, value, profile in selections}
    if len(selections) == 2 and {value for value, _ in values} == {"real"}:
        return "integration-voice", [variable for variable, _, _ in selections]
    profiles = {profile for _, _, profile in selections}
    if len(profiles) != 1:
        names = ", ".join(variable for variable, _, _ in selections)
        raise ProfileError(f"legacy selectors cannot form one profile: {names}")
    return next(iter(profiles)), [variable for variable, _, _ in selections]


def _select_profile(env: dict[str, str], default_profile: str | None) -> tuple[str, str, list[str]]:
    legacy_name, used_legacy = _legacy_profile(env)
    if PROFILE_ENV in env:
        requested = env[PROFILE_ENV]
        if not requested:
            raise ProfileError(f"{PROFILE_ENV} must not be empty")
        if legacy_name is not None:
            requested_dependencies = load_profile(requested)["dependencies"]
            legacy_dependencies = load_profile(legacy_name)["dependencies"]
            if requested_dependencies != legacy_dependencies:
                variables = ", ".join(used_legacy)
                raise ProfileError(f"{PROFILE_ENV} conflicts with {variables}")
        return requested, PROFILE_ENV, used_legacy
    if legacy_name is not None:
        return legacy_name, "legacy-environment", used_legacy
    if default_profile is not None:
        return default_profile, "default-profile", []
    raise ProfileError("profile selection requires DS_PROFILE, a legacy selector, or --default-profile")


def _copy_dependency(dependency: Dependency) -> Dependency:
    return cast(Dependency, {
        "mode": dependency["mode"],
        "source": dependency["source"],
        **({"baseUrl": dependency["baseUrl"]} if "baseUrl" in dependency else {}),
        **(
            {"readinessPath": dependency["readinessPath"]}
            if "readinessPath" in dependency
            else {}
        ),
    })


def _apply_backend_override(profile: Profile, env: dict[str, str]) -> tuple[Dependencies, list[str]]:
    profile_dependencies = cast(dict[str, Dependency], profile["dependencies"])
    dependencies = {
        name: _copy_dependency(dependency)
        for name, dependency in profile_dependencies.items()
    }
    if BACKEND_ORIGIN_ENV not in env:
        return cast(Dependencies, dependencies), []
    origin = validate_http_origin(env[BACKEND_ORIGIN_ENV], BACKEND_ORIGIN_ENV)
    backend = profile_dependencies["backend"]
    if backend["mode"] != "real":
        raise ProfileError(f"{BACKEND_ORIGIN_ENV} requires a real backend")
    overridden = {
        **dependencies,
        "backend": {**backend, "source": "external", "baseUrl": origin},
    }
    return cast(Dependencies, overridden), [BACKEND_ORIGIN_ENV]


def _resolve_dependency(dependency: Dependency) -> ResolvedDependency:
    has_readiness = "baseUrl" in dependency and "readinessPath" in dependency
    readiness = (
        {
            "readinessUrl": dependency["baseUrl"].rstrip("/")
            + dependency["readinessPath"]
        }
        if has_readiness
        else {}
    )
    return cast(ResolvedDependency, {**dependency, **readiness})


def resolve_dependencies(dependencies: Dependencies) -> ResolvedDependencies:
    dependency_map = cast(dict[str, Dependency], dependencies)
    return cast(ResolvedDependencies, {
        name: _resolve_dependency(dependency)
        for name, dependency in dependency_map.items()
    })


def derive_capabilities(dependencies: ResolvedDependencies) -> list[Capability]:
    frontend_real = dependencies["frontend"]["mode"] == "real"
    backend = dependencies["backend"]
    text_real = (
        frontend_real
        and backend["mode"] == "real"
        and dependencies["ollama"]["mode"] == "real"
    )
    enabled: tuple[tuple[Capability, bool], ...] = (
        (
            "mocked-e2e",
            frontend_real and backend["mode"] == "mock" and backend["source"] == "browser",
        ),
        ("text-chat-real", text_real),
        (
            "voice-chat-real",
            text_real
            and dependencies["voicevox"]["mode"] == "real"
            and dependencies["whisper"]["mode"] == "real",
        ),
        ("rag-real", text_real and dependencies["chroma"]["mode"] == "real"),
    )
    return [capability for capability, is_enabled in enabled if is_enabled]


def derive_environment(dependencies: ResolvedDependencies) -> dict[str, str]:
    dependency_map = cast(dict[str, ResolvedDependency], dependencies)
    real_service_urls = {
        environment_name: dependency_map[dependency_name]["baseUrl"]
        for dependency_name, environment_name in (
            ("ollama", OLLAMA_BASE_URL_ENV),
            ("voicevox", VOICEVOX_BASE_URL_ENV),
            ("backend", BACKEND_DERIVED_ORIGIN_ENV),
        )
        if dependency_map[dependency_name]["mode"] == "real"
    }
    return {
        RAG_ENABLED_ENV: str(dependencies["chroma"]["mode"] == "real").lower(),
        **real_service_urls,
    }


def _compatibility(env: dict[str, str], used: list[str]) -> Compatibility:
    report_variables = [LEGACY_REPORT_ENV] if LEGACY_REPORT_ENV in env else []
    used_variables = [*used, *report_variables]
    warnings = [
        f"{name} is deprecated; use DS_PROFILE and DS_PROFILE_REPORT"
        for name in used_variables
    ]
    return {"usedEnvironmentVariables": used_variables, "warnings": warnings}


def resolve_profile(env: dict[str, str], default_profile: str | None) -> ResolvedReport:
    selected, source, used_legacy = _select_profile(env, default_profile)
    profile = load_profile(selected)
    dependencies, used_override = _apply_backend_override(profile, env)
    resolved_dependencies = resolve_dependencies(dependencies)
    return {
        "reportSchemaVersion": REPORT_SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
        "requestedProfile": selected,
        "effectiveProfile": selected,
        "selectionSource": source,
        "profile": {"schemaVersion": 1, "name": profile["name"]},
        "dependencies": resolved_dependencies,
        "capabilities": derive_capabilities(resolved_dependencies),
        "derivedEnvironment": derive_environment(resolved_dependencies),
        "compatibility": _compatibility(env, [*used_legacy, *used_override]),
    }

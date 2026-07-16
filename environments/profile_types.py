from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


DependencyMode = Literal["real", "mock", "disabled"]
DependencySource = Literal["managed", "external", "in_process", "browser"] | None
DependencyName = Literal["frontend", "backend", "ollama", "voicevox", "whisper", "chroma"]
Capability = Literal["mocked-e2e", "text-chat-real", "voice-chat-real", "rag-real"]


class ProfileError(Exception):
    """利用者が修正できるProfile契約違反。"""


class Dependency(TypedDict):
    mode: DependencyMode
    source: DependencySource
    baseUrl: NotRequired[str]
    readinessPath: NotRequired[str]


class ResolvedDependency(Dependency):
    readinessUrl: NotRequired[str]


class Dependencies(TypedDict):
    frontend: Dependency
    backend: Dependency
    ollama: Dependency
    voicevox: Dependency
    whisper: Dependency
    chroma: Dependency


class ResolvedDependencies(TypedDict):
    frontend: ResolvedDependency
    backend: ResolvedDependency
    ollama: ResolvedDependency
    voicevox: ResolvedDependency
    whisper: ResolvedDependency
    chroma: ResolvedDependency


class Profile(TypedDict):
    schemaVersion: Literal[1]
    name: str
    description: str
    dependencies: Dependencies


class ProfileIdentity(TypedDict):
    schemaVersion: Literal[1]
    name: str


class Compatibility(TypedDict):
    usedEnvironmentVariables: list[str]
    warnings: list[str]


class ResolvedReport(TypedDict):
    reportSchemaVersion: Literal[1]
    generatedAt: str
    requestedProfile: str
    effectiveProfile: str
    selectionSource: str
    profile: ProfileIdentity
    dependencies: ResolvedDependencies
    capabilities: list[Capability]
    derivedEnvironment: dict[str, str]
    compatibility: Compatibility


class LegacyBackendReport(TypedDict):
    mode: DependencyMode
    reasons: list[str]

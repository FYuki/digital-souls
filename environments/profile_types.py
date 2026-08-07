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
    reload: NotRequired[bool]


class ResolvedDependency(Dependency):
    readinessUrl: NotRequired[str]
    host: NotRequired[str]
    port: NotRequired[int]


class ReadyGate(TypedDict):
    baseUrl: str


class ResolvedReadyGate(ReadyGate):
    host: str
    port: int


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
    readyGate: ReadyGate
    dependencies: Dependencies


class ProfileIdentity(TypedDict):
    schemaVersion: Literal[1]
    name: str


class Compatibility(TypedDict):
    usedEnvironmentVariables: list[str]
    warnings: list[str]


class RuntimeProjection(TypedDict):
    environmentId: str
    dataRoot: str
    sqlitePath: str
    chromaPath: str
    runtimeReportDirectory: str
    cachePath: str


class ResolvedReport(TypedDict):
    reportSchemaVersion: Literal[1]
    generatedAt: str
    requestedProfile: str
    effectiveProfile: str
    selectionSource: str
    profile: ProfileIdentity
    readyGate: ResolvedReadyGate
    dependencies: ResolvedDependencies
    capabilities: list[Capability]
    derivedEnvironment: dict[str, str]
    runtime: RuntimeProjection
    compatibility: Compatibility


class LegacyBackendReport(TypedDict):
    mode: DependencyMode
    reasons: list[str]

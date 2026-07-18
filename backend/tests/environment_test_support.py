from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import cast

from profile_types import ResolvedDependencies, ResolvedDependency, ResolvedReport


DEPENDENCY_NAMES = ("frontend", "backend", "ollama", "voicevox", "whisper", "chroma")


def orchestrator_identity() -> dict[str, int]:
    return {
        "pid": 2_147_483_647,
        "pgid": 2_147_483_647,
        "sessionId": 2_147_483_647,
        "startTime": 1,
    }


def resolved_profile(profile_name: str = "integration-voice") -> ResolvedReport:
    dependencies: dict[str, ResolvedDependency] = {
        "frontend": {
            "mode": "real",
            "source": "managed",
            "baseUrl": "http://localhost:5173",
            "readinessPath": "/",
            "readinessUrl": "http://localhost:5173/",
        },
        "backend": {
            "mode": "real",
            "source": "managed",
            "baseUrl": "http://localhost:8000",
            "readinessPath": "/",
            "readinessUrl": "http://localhost:8000/",
        },
        "ollama": {
            "mode": "real",
            "source": "managed",
            "baseUrl": "http://localhost:11434",
            "readinessPath": "/api/tags",
            "readinessUrl": "http://localhost:11434/api/tags",
        },
        "voicevox": {
            "mode": "real",
            "source": "managed",
            "baseUrl": "http://localhost:50021",
            "readinessPath": "/version",
            "readinessUrl": "http://localhost:50021/version",
        },
        "whisper": {"mode": "real", "source": "in_process"},
        "chroma": {"mode": "disabled", "source": None},
    }
    return cast(ResolvedReport, {
        "reportSchemaVersion": 1,
        "generatedAt": "2026-07-17T00:00:00+00:00",
        "requestedProfile": profile_name,
        "effectiveProfile": profile_name,
        "selectionSource": "DS_PROFILE",
        "profile": {"schemaVersion": 1, "name": profile_name},
        "dependencies": dependencies,
        "capabilities": ["text-chat-real", "voice-chat-real"],
        "derivedEnvironment": {
            "OLLAMA_BASE_URL": "http://localhost:11434",
            "VOICEVOX_BASE_URL": "http://localhost:50021",
            "DS_BACKEND_ORIGIN": "http://localhost:8000",
            "RAG_ENABLED": "false",
        },
        "compatibility": {"usedEnvironmentVariables": [], "warnings": []},
    })


def profile_with_dependencies(**overrides: ResolvedDependency) -> ResolvedReport:
    profile = deepcopy(resolved_profile())
    profile["dependencies"] = cast(
        ResolvedDependencies,
        {**profile["dependencies"], **overrides},
    )
    return profile


class RecordingRunner:
    def __init__(self, responses: list[dict[str, object]] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.responses = list([] if responses is None else responses)

    def run(self, command: tuple[str, ...], cwd: Path) -> dict[str, object]:
        self.calls.append(command)
        if self.responses:
            return self.responses.pop(0)
        return {"returncode": 0, "stdout": "", "stderr": ""}

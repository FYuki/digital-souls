from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, cast

from profile_types import ResolvedDependencies, ResolvedDependency, ResolvedReport

if TYPE_CHECKING:
    from adapters.base import ServiceOperations
    from service_registry import ServiceRegistry


def runtime_projection(data_root: Path = Path("/test/runtime-data")) -> dict[str, str]:
    return {
        "environmentId": "test",
        "dataRoot": str(data_root),
        "sqlitePath": str(data_root / "conversation-history.db"),
        "chromaPath": str(data_root / "chroma"),
        "runtimeReportDirectory": str(data_root / "runtime"),
        "cachePath": str(data_root / "cache"),
    }


def resolved_runtime_paths(root_dir: Path):
    from app.runtime_paths import resolve_runtime_paths

    return resolve_runtime_paths(
        {
            "DS_ENVIRONMENT_ID": "test",
            "DS_DATA_DIR": str(root_dir / "runtime-data"),
        },
        root_dir,
    )


def single_adapter_registry(
    service: str, adapter: ServiceOperations
) -> ServiceRegistry:
    from environment_constants import DEPENDENCY_NAMES
    from service_registry import ServiceRegistration, ServiceRegistry

    return ServiceRegistry(
        services={
            name: ServiceRegistration(
                name,
                adapter if name == service else None,
                "backend" if name in {"whisper", "chroma"} else None,
            )
            for name in DEPENDENCY_NAMES
        },
        prepare_order=(service,),
        start_order=(service,),
    )


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
            "host": "localhost",
            "port": 5173,
        },
        "backend": {
            "mode": "real",
            "source": "managed",
            "baseUrl": "http://localhost:8000",
            "readinessPath": "/",
            "readinessUrl": "http://localhost:8000/",
            "host": "localhost",
            "port": 8000,
            "reload": True,
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
        "readyGate": {
            "baseUrl": "http://127.0.0.1:4174",
            "host": "127.0.0.1",
            "port": 4174,
        },
        "dependencies": dependencies,
        "capabilities": ["text-chat-real", "voice-chat-real"],
        "derivedEnvironment": {
            "OLLAMA_BASE_URL": "http://localhost:11434",
            "VOICEVOX_BASE_URL": "http://localhost:50021",
            "DS_BACKEND_ORIGIN": "http://localhost:8000",
            "RAG_ENABLED": "false",
            "OLLAMA_CHAT_MODEL": "gemma4:e4b",
            "WHISPER_MODEL": "medium",
            "OLLAMA_CONTEXT_TOKENS": "8192",
            "OLLAMA_RESPONSE_RESERVE_TOKENS": "1024",
            "ASSISTANT_MAX_GENERATION_TOKENS": "1024",
            "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": "10",
            "CONVERSATION_HISTORY_TOKEN_LIMIT": "4096",
            "USER_INPUT_TOKEN_LIMIT": "8192",
            "LLM_CONTEXT_TOKEN_LIMIT": "32768",
            "DS_ENVIRONMENT_ID": "test",
            "DS_DATA_DIR": "/test/runtime-data",
        },
        "runtime": runtime_projection(),
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

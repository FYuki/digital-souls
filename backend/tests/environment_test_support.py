from __future__ import annotations

import shlex
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
        "personaMemorySqlitePath": str(data_root / "persona-memory.db"),
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


def write_cached_whisper_model(
    root_dir: Path, repository_id: str, *, complete: bool = True
) -> Path:
    python = root_dir / "backend" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    repository_cache = (
        root_dir
        / "runtime-data"
        / "cache"
        / "huggingface"
        / "hub"
        / f"models--{repository_id.replace('/', '--')}"
    )
    snapshot = repository_cache / "snapshots" / "revision"
    python.write_text(
        f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(str(snapshot))}\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    snapshot.mkdir(parents=True)
    refs = repository_cache / "refs"
    refs.mkdir()
    (refs / "main").write_text("revision", encoding="utf-8")
    if complete:
        for artifact in (
            "config.json",
            "model.bin",
            "preprocessor_config.json",
            "tokenizer.json",
            "vocabulary.json",
        ):
            (snapshot / artifact).write_text("fixture", encoding="utf-8")
    return snapshot


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
            "readinessPath": "/health/ready",
            "readinessUrl": "http://localhost:8000/health/ready",
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
        "livekit": {
            "mode": "real",
            "source": "external",
            "baseUrl": "http://127.0.0.1:7880",
            "readinessPath": "/",
            "readinessUrl": "http://127.0.0.1:7880/",
        },
        "whisper": {
            "mode": "real",
            "source": "external",
            "baseUrl": "http://127.0.0.1:50022",
            "readinessPath": "/health/ready",
            "readinessUrl": "http://127.0.0.1:50022/health/ready",
        },
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
            "WHISPER_BASE_URL": "http://127.0.0.1:50022",
            "DS_BACKEND_ORIGIN": "http://localhost:8000",
            "RAG_ENABLED": "false",
            "WHISPER_MODEL": "medium",
            "INFERENCE_TARGET_CHAT": "ollama/gemma4:e4b",
            "INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS": "7168",
            "INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS": "1024",
            "INFERENCE_TARGET_PRIVACY": "ollama/gemma4:e4b",
            "INFERENCE_TARGET_PRIVACY_MAX_INPUT_TOKENS": "7680",
            "INFERENCE_TARGET_PRIVACY_MAX_OUTPUT_TOKENS": "512",
            "INFERENCE_TARGET_MEMORY_EXTRACTION": "ollama/gemma4:e4b",
            "INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_INPUT_TOKENS": "7680",
            "INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS": "512",
            "INFERENCE_TARGET_MEMORY_CONSOLIDATION": "ollama/gemma4:e4b",
            "INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_INPUT_TOKENS": "7680",
            "INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_OUTPUT_TOKENS": "512",
            "INFERENCE_TARGET_EMBEDDING": "ollama/nomic-embed-text:latest",
            "INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS": "8192",
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
        self.cwds: list[Path] = []
        self.responses = list([] if responses is None else responses)

    def run(self, command: tuple[str, ...], cwd: Path) -> dict[str, object]:
        self.calls.append(command)
        self.cwds.append(cwd)
        if self.responses:
            return self.responses.pop(0)
        return {"returncode": 0, "stdout": "", "stderr": ""}

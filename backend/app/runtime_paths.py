from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


ENVIRONMENT_ID_ENV = "DS_ENVIRONMENT_ID"
DATA_DIR_ENV = "DS_DATA_DIR"
IDENTITY_MARKER_FILENAME = ".environment-identity.json"
RESTORE_INTENT_FILENAME = ".sqlite-restore-intent.json"
SQLITE_FILENAME = "conversation-history.db"
PERSONA_MEMORY_SQLITE_FILENAME = "persona-memory.db"
CHROMA_DIRECTORY = "chroma"
RUNTIME_DIRECTORY = "runtime"
CACHE_DIRECTORY = "cache"
WHISPER_CACHE_PARTS = ("huggingface", "hub")
SUPPORTED_ENVIRONMENT_IDS = frozenset({"dev", "test", "dogfood"})


@dataclass(frozen=True)
class RuntimePaths:
    environment_id: str
    data_root: Path
    sqlite_path: Path
    persona_memory_sqlite_path: Path
    chroma_path: Path
    runtime_report_dir: Path
    cache_path: Path
    whisper_cache_path: Path
    identity_marker_path: Path
    restore_intent_path: Path


def resolve_runtime_paths(
    environment: Mapping[str, str], repository_root: Path
) -> RuntimePaths:
    normalized_repository_root = repository_root.resolve()
    environment_id = _resolve_environment_id(environment)
    data_root = _resolve_data_root(
        environment, normalized_repository_root, environment_id
    )
    cache_path = data_root / CACHE_DIRECTORY
    return RuntimePaths(
        environment_id=environment_id,
        data_root=data_root,
        sqlite_path=data_root / SQLITE_FILENAME,
        persona_memory_sqlite_path=data_root / PERSONA_MEMORY_SQLITE_FILENAME,
        chroma_path=data_root / CHROMA_DIRECTORY,
        runtime_report_dir=data_root / RUNTIME_DIRECTORY,
        cache_path=cache_path,
        whisper_cache_path=cache_path.joinpath(*WHISPER_CACHE_PARTS),
        identity_marker_path=data_root / IDENTITY_MARKER_FILENAME,
        restore_intent_path=data_root / RESTORE_INTENT_FILENAME,
    )


def runtime_paths_projection(paths: RuntimePaths) -> dict[str, str]:
    return {
        "environmentId": paths.environment_id,
        "dataRoot": str(paths.data_root),
        "sqlitePath": str(paths.sqlite_path),
        "personaMemorySqlitePath": str(paths.persona_memory_sqlite_path),
        "chromaPath": str(paths.chroma_path),
        "runtimeReportDirectory": str(paths.runtime_report_dir),
        "cachePath": str(paths.cache_path),
    }


def _resolve_environment_id(environment: Mapping[str, str]) -> str:
    environment_id = environment.get(ENVIRONMENT_ID_ENV)
    if environment_id is None:
        return "dev"
    if environment_id not in SUPPORTED_ENVIRONMENT_IDS:
        raise ValueError(
            f"{ENVIRONMENT_ID_ENV} must be one of dev, test, dogfood"
        )
    return environment_id


def _resolve_data_root(
    environment: Mapping[str, str], repository_root: Path, environment_id: str
) -> Path:
    configured = environment.get(DATA_DIR_ENV)
    candidate = (
        repository_root / "backend" / "app" / "data"
        if configured is None
        else Path(configured)
    )
    if not candidate.is_absolute():
        raise ValueError(f"{DATA_DIR_ENV} must be an absolute path")
    _reject_symlink_components(candidate)
    normalized = candidate.resolve(strict=False)
    dangerous_roots = {Path("/"), Path.home().resolve(), repository_root}
    if normalized in dangerous_roots:
        raise ValueError(f"{DATA_DIR_ENV} points to a dangerous broad path")
    if environment_id == "dogfood" and normalized.is_relative_to(repository_root):
        raise ValueError("dogfood data root must be outside the repository")
    return normalized


def _reject_symlink_components(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise ValueError(f"{DATA_DIR_ENV} must not contain symlinks")
        current = current.parent

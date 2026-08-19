from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Final, Iterator

from app.runtime_paths import RuntimePaths, SUPPORTED_ENVIRONMENT_IDS
from app.conversation_history.sqlite_lease import (
    PERSONA_MEMORY_SQLITE_LEASE_FILENAME_SUFFIX,
    SQLITE_LEASE_FILENAME_SUFFIX,
    ensure_sqlite_lease_file,
)


MARKER_SCHEMA_VERSION: Final = 1
IDENTITY_LOCK_FILENAME = ".environment-identity.lock"
_CHROMA_INDEX_CUTOVER_MARKER_FILENAME = ".legacy-chroma-index-removed"
IGNORED_SCAFFOLDING_NAMES = frozenset(
    {
        ".gitkeep",
        IDENTITY_LOCK_FILENAME,
        SQLITE_LEASE_FILENAME_SUFFIX,
        PERSONA_MEMORY_SQLITE_LEASE_FILENAME_SUFFIX,
    }
)


def initialize_runtime_data_root(
    paths: RuntimePaths, repository_root: Path
) -> None:
    _validate_path_contract(paths, repository_root)
    if paths.identity_marker_path.exists():
        _validate_identity_marker(paths)
    with _identity_lock(paths.data_root):
        if paths.identity_marker_path.exists():
            _validate_identity_marker(paths)
        else:
            if _contains_persistent_data(paths.data_root):
                raise ValueError("runtime data root identity marker is missing")
            _create_identity_marker(paths)
        ensure_sqlite_lease_file(paths.sqlite_path)
        ensure_sqlite_lease_file(paths.persona_memory_sqlite_path)


def validate_existing_runtime_data_root(
    paths: RuntimePaths, repository_root: Path
) -> None:
    _validate_path_contract(paths, repository_root)
    if not paths.identity_marker_path.exists():
        raise ValueError("runtime data root identity marker is missing")
    _validate_identity_marker(paths)


def remove_legacy_chroma_index_once(
    paths: RuntimePaths,
    repository_root: Path,
) -> None:
    _validate_path_contract(paths, repository_root)
    completion_marker = paths.data_root / _CHROMA_INDEX_CUTOVER_MARKER_FILENAME
    with _identity_lock(paths.data_root):
        if completion_marker.exists():
            if not completion_marker.is_file():
                raise ValueError("Chroma index cutover marker must be a file")
            return
        chroma_path = paths.chroma_path
        if chroma_path.is_dir():
            shutil.rmtree(chroma_path)
        elif chroma_path.exists():
            chroma_path.unlink()
        _create_chroma_index_cutover_marker(completion_marker)


def validate_runtime_projection(
    projection: object, paths: RuntimePaths
) -> None:
    from app.runtime_paths import runtime_paths_projection

    current_projection = runtime_paths_projection(paths)
    legacy_projection = {
        key: value
        for key, value in current_projection.items()
        if key != "personaMemorySqlitePath"
    }
    if projection not in (current_projection, legacy_projection):
        raise ValueError("runtime projection does not match resolved runtime paths")


def _validate_path_contract(paths: RuntimePaths, repository_root: Path) -> None:
    if paths.environment_id not in SUPPORTED_ENVIRONMENT_IDS:
        raise ValueError("runtime environment identity is invalid")
    data_root = paths.data_root
    if data_root.is_symlink():
        raise ValueError("runtime data root must not be a symlink")
    if data_root.exists() and not data_root.is_dir():
        raise ValueError("runtime data root must be a directory")
    if not data_root.exists():
        parent = _nearest_existing_parent(data_root)
        if not os.access(parent, os.W_OK | os.X_OK):
            raise ValueError("runtime data root parent must be writable")
        data_root.mkdir(parents=True, exist_ok=True)
    if not os.access(data_root, os.W_OK | os.X_OK):
        raise ValueError("runtime data root must be writable")
    normalized_repository = repository_root.resolve()
    if paths.environment_id == "dogfood" and data_root.is_relative_to(
        normalized_repository
    ):
        raise ValueError("dogfood data root must be outside the repository")
    _validate_derived_paths(paths)


def _validate_derived_paths(paths: RuntimePaths) -> None:
    derived_paths = (
        paths.sqlite_path,
        paths.persona_memory_sqlite_path,
        paths.chroma_path,
        paths.runtime_report_dir,
        paths.cache_path,
        paths.whisper_cache_path,
        paths.identity_marker_path,
        paths.restore_intent_path,
        paths.data_root / _CHROMA_INDEX_CUTOVER_MARKER_FILENAME,
        paths.data_root / IDENTITY_LOCK_FILENAME,
        paths.data_root / SQLITE_LEASE_FILENAME_SUFFIX,
        paths.data_root / PERSONA_MEMORY_SQLITE_LEASE_FILENAME_SUFFIX,
    )
    for path in derived_paths:
        _validate_derived_path(path, paths.data_root)


def _validate_derived_path(path: Path, data_root: Path) -> None:
    if not path.is_relative_to(data_root):
        raise ValueError("runtime derived path must be inside the data root")
    current = path
    while current != data_root:
        if current.is_symlink():
            raise ValueError("runtime derived path must not contain symlinks")
        current = current.parent
    canonical_root = data_root.resolve(strict=False)
    if not path.resolve(strict=False).is_relative_to(canonical_root):
        raise ValueError("runtime derived path must be inside the data root")


def _nearest_existing_parent(path: Path) -> Path:
    current = path.parent
    while not current.exists():
        current = current.parent
    if not current.is_dir():
        raise ValueError("runtime data root parent must be a directory")
    return current


def _contains_persistent_data(data_root: Path) -> bool:
    return any(entry.name not in IGNORED_SCAFFOLDING_NAMES for entry in data_root.iterdir())


@contextmanager
def _identity_lock(data_root: Path) -> Iterator[None]:
    lock_path = data_root / IDENTITY_LOCK_FILENAME
    with lock_path.open("a", encoding="utf-8") as lock:
        flock(lock.fileno(), LOCK_EX)
        try:
            yield
        finally:
            flock(lock.fileno(), LOCK_UN)


def _marker_value(paths: RuntimePaths) -> dict[str, object]:
    return {
        "schemaVersion": MARKER_SCHEMA_VERSION,
        "environmentId": paths.environment_id,
    }


def _create_identity_marker(paths: RuntimePaths) -> None:
    payload = json.dumps(_marker_value(paths), ensure_ascii=False) + "\n"
    try:
        descriptor = os.open(
            paths.identity_marker_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        _validate_identity_marker(paths)
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as marker:
        marker.write(payload)
        marker.flush()
        os.fsync(marker.fileno())


def _create_chroma_index_cutover_marker(marker_path: Path) -> None:
    descriptor = os.open(
        marker_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as marker:
        marker.write("completed\n")
        marker.flush()
        os.fsync(marker.fileno())


def _validate_identity_marker(paths: RuntimePaths) -> None:
    try:
        marker = json.loads(paths.identity_marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("runtime data root identity marker is invalid") from error
    expected_fields = {"schemaVersion", "environmentId"}
    if not isinstance(marker, dict) or set(marker) != expected_fields:
        raise ValueError("runtime data root identity marker is invalid")
    marker_id = marker["environmentId"]
    if (
        marker["schemaVersion"] != MARKER_SCHEMA_VERSION
        or isinstance(marker["schemaVersion"], bool)
        or marker_id not in SUPPORTED_ENVIRONMENT_IDS
    ):
        raise ValueError("runtime data root identity marker is invalid")
    if marker_id != paths.environment_id:
        raise ValueError("runtime data root environment identity does not match")

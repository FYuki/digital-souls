from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Protocol, cast

from app.memory.persistence.contracts import TemporalPrecision


class RetrievalMatchKind(str, Enum):
    BOTH = "BOTH"
    SEMANTIC = "SEMANTIC"
    PERIOD = "PERIOD"


COLLECTION_NAME_PREFIX = "character"
COLLECTION_NAME_MAX_LENGTH = 63
COLLECTION_NAME_DIGEST_LENGTH = 12
COLLECTION_NAME_SEPARATOR_COUNT = 2
COLLECTION_NAME_MAX_SLUG_LENGTH = (
    COLLECTION_NAME_MAX_LENGTH
    - len(COLLECTION_NAME_PREFIX)
    - COLLECTION_NAME_DIGEST_LENGTH
    - COLLECTION_NAME_SEPARATOR_COUNT
)
INDEX_STATE_FILENAME = "memory-index-state-v1.json"


@dataclass(frozen=True)
class EmbeddingFingerprint:
    provider_id: str
    model_id: str
    dimension: int

    def __post_init__(self) -> None:
        if not self.provider_id or self.provider_id.strip() != self.provider_id:
            raise ValueError("embedding provider id must be canonical")
        if not self.model_id or self.model_id.strip() != self.model_id:
            raise ValueError("embedding model id must be canonical")
        if type(self.dimension) is not int or self.dimension < 1:
            raise ValueError("embedding dimension must be a positive integer")

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.as_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:COLLECTION_NAME_DIGEST_LENGTH]

    def as_dict(self) -> dict[str, str | int]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "dimension": self.dimension,
        }


class MemoryIndexReindexRequiredError(RuntimeError):
    """現在のEmbedding設定に対応するIndexがまだ有効化されていない。"""


@dataclass(frozen=True)
class MemorySearchCandidate:
    memory_id: str
    raw_distance: float


@dataclass(frozen=True)
class MemorySearchResult:
    memory_id: str
    normalized_text: str
    occurred_at: str | None
    memory_type: str
    raw_distance: float
    occurred_precision: TemporalPrecision | None = None
    match_kind: RetrievalMatchKind = RetrievalMatchKind.SEMANTIC


class _ChromaCollection(Protocol):
    def upsert(
        self,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, str]],
    ) -> None: ...

    def delete(self, *, ids: list[str]) -> None: ...

    def get(
        self,
        *,
        ids: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        include: list[str] | None = None,
    ) -> dict[str, object]: ...

    def query(
        self,
        *,
        query_embeddings: list[list[float]],
        n_results: int,
        include: list[str],
    ) -> dict[str, object]: ...


class _ChromaClient(Protocol):
    def get_or_create_collection(self, name: str) -> _ChromaCollection: ...

    def delete_collection(self, name: str) -> None: ...


def upsert_memory_index_entry(
    *,
    character_id: str,
    memory_id: str,
    embedding: list[float],
    normalized_text: str,
    provider_id: str,
    memory_kind: str,
    memory_type: str,
    policy_version: str,
    occurred_at: str | None,
    expires_at: str | None,
    chroma_path: Path,
    fingerprint: EmbeddingFingerprint | None = None,
) -> None:
    metadata = memory_index_metadata(
        character_id=character_id,
        provider_id=provider_id,
        memory_kind=memory_kind,
        memory_type=memory_type,
        policy_version=policy_version,
        occurred_at=occurred_at,
        expires_at=expires_at,
        fingerprint=fingerprint,
    )
    collection = _collection(character_id, chroma_path, fingerprint)
    # Chroma の版によらず、訂正前の metadata キーを残さないため置換する。
    collection.delete(ids=[memory_id])
    collection.upsert(
        ids=[memory_id],
        embeddings=[embedding],
        documents=[normalized_text],
        metadatas=[metadata],
    )


def delete_memory_index_entry(
    *,
    character_id: str,
    memory_id: str,
    chroma_path: Path,
    fingerprint: EmbeddingFingerprint | None = None,
) -> None:
    _collection(character_id, chroma_path, fingerprint).delete(ids=[memory_id])


def delete_memory_index_collection(
    *,
    character_id: str,
    chroma_path: Path,
    fingerprint: EmbeddingFingerprint | None = None,
) -> None:
    collection_name = _collection_name(character_id, fingerprint)
    try:
        _client(str(chroma_path)).delete_collection(name=collection_name)
    except Exception as exc:
        if not _is_missing_collection_error(exc):
            raise


def list_memory_index_ids(
    *,
    character_id: str,
    chroma_path: Path,
    fingerprint: EmbeddingFingerprint | None = None,
) -> set[str]:
    result = _collection(character_id, chroma_path, fingerprint).get(include=[])
    return set(_flat_string_list(result, "ids"))


def get_memory_index_metadata(
    *,
    character_id: str,
    memory_id: str,
    chroma_path: Path,
    fingerprint: EmbeddingFingerprint | None = None,
) -> dict[str, str] | None:
    result = _collection(character_id, chroma_path, fingerprint).get(ids=[memory_id])
    ids = _flat_string_list(result, "ids")
    if not ids:
        return None
    metadatas = _flat_object_list(result, "metadatas")
    if len(ids) != 1 or len(metadatas) != 1:
        raise ValueError("Chroma get result ids and metadatas must match")
    metadata = metadatas[0]
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in metadata.items()
    ):
        raise ValueError("Chroma memory metadata must contain only strings")
    return cast(dict[str, str], metadata)


def query_memories(
    character: str,
    embedding: list[float],
    n_results: int,
    *,
    chroma_path: Path,
    fingerprint: EmbeddingFingerprint | None = None,
) -> list[MemorySearchCandidate]:
    if fingerprint is not None and (
        active_memory_index_fingerprint(character, chroma_path) != fingerprint
    ):
        raise MemoryIndexReindexRequiredError("memory index requires reindexing")
    collection = _collection(character, chroma_path, fingerprint)
    return _query_memory_candidates(
        collection, embedding=embedding, n_results=n_results
    )


def _query_memory_candidates(
    collection: _ChromaCollection,
    *,
    embedding: list[float],
    n_results: int,
) -> list[MemorySearchCandidate]:
    result = collection.query(
        query_embeddings=[embedding],
        n_results=n_results,
        include=["distances"],
    )
    ids = _first_result_list(result, "ids")
    distances = _first_result_list(result, "distances")
    if len(ids) != len(distances):
        raise ValueError("Chroma query result ids and distances must match")
    if not ids:
        return []
    return [
        _memory_search_candidate(memory_id, distance)
        for memory_id, distance in zip(ids, distances, strict=True)
    ]


def _collection(
    character: str,
    chroma_path: Path,
    fingerprint: EmbeddingFingerprint | None = None,
) -> _ChromaCollection:
    collection_name = _collection_name(character, fingerprint)
    chroma_path.mkdir(parents=True, exist_ok=True)
    return _client(str(chroma_path)).get_or_create_collection(name=collection_name)


@lru_cache(maxsize=None)
def _client(chroma_path: str) -> _ChromaClient:
    chromadb = importlib.import_module("chromadb")
    return cast(_ChromaClient, chromadb.PersistentClient(path=chroma_path))


def _is_missing_collection_error(error: Exception) -> bool:
    if isinstance(error, ValueError):
        return "does not exist" in str(error)
    if error.__class__.__name__ != "NotFoundError":
        return False
    chroma_errors = importlib.import_module("chromadb.errors")
    not_found_error = getattr(chroma_errors, "NotFoundError", None)
    return isinstance(not_found_error, type) and isinstance(error, not_found_error)


def memory_index_metadata(
    *,
    character_id: str,
    provider_id: str,
    memory_kind: str,
    memory_type: str,
    policy_version: str,
    occurred_at: str | None,
    expires_at: str | None,
    fingerprint: EmbeddingFingerprint | None = None,
) -> dict[str, str]:
    return {
        "character_id": character_id,
        "provider_id": provider_id,
        "memory_kind": memory_kind,
        "memory_type": memory_type,
        "policy_version": policy_version,
        **({"occurred_at": occurred_at} if occurred_at is not None else {}),
        **({"expires_at": expires_at} if expires_at is not None else {}),
        **(
            {
                "embedding_provider_id": fingerprint.provider_id,
                "embedding_model_id": fingerprint.model_id,
                "embedding_dimension": str(fingerprint.dimension),
            }
            if fingerprint is not None
            else {}
        ),
    }


def _collection_name(
    character: str, fingerprint: EmbeddingFingerprint | None = None
) -> str:
    normalized = character.strip()
    if not normalized:
        raise ValueError("character must not be empty")
    slug = re.sub(r"[^a-z0-9_-]+", "-", normalized.lower()).strip("-_")
    suffix_length = (
        0
        if fingerprint is None
        else len(fingerprint.digest) + COLLECTION_NAME_SEPARATOR_COUNT - 1
    )
    slug = slug[: COLLECTION_NAME_MAX_SLUG_LENGTH - suffix_length].strip("-_")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[
        :COLLECTION_NAME_DIGEST_LENGTH
    ]
    base = (
        f"{COLLECTION_NAME_PREFIX}-{slug}-{digest}"
        if slug
        else f"{COLLECTION_NAME_PREFIX}-{digest}"
    )
    return base if fingerprint is None else f"{base}-{fingerprint.digest}"


def active_memory_index_fingerprint(
    character_id: str, chroma_path: Path
) -> EmbeddingFingerprint | None:
    state = _read_index_state(chroma_path)
    raw = state.get(character_id)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("memory index state entry must be an object")
    if set(raw) != {"provider_id", "model_id", "dimension"}:
        raise ValueError("memory index fingerprint fields are invalid")
    provider_id = raw["provider_id"]
    model_id = raw["model_id"]
    dimension = raw["dimension"]
    if not isinstance(provider_id, str) or not isinstance(model_id, str):
        raise ValueError("memory index fingerprint ids must be strings")
    if type(dimension) is not int:
        raise ValueError("memory index fingerprint dimension must be an integer")
    return EmbeddingFingerprint(provider_id, model_id, dimension)


def activate_memory_index(
    character_id: str,
    fingerprint: EmbeddingFingerprint,
    chroma_path: Path,
) -> None:
    state = _read_index_state(chroma_path)
    state[character_id] = fingerprint.as_dict()
    chroma_path.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=chroma_path,
            prefix=f".{INDEX_STATE_FILENAME}.",
            delete=False,
        ) as temporary:
            json.dump(
                {"version": 1, "characters": state},
                temporary,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, chroma_path / INDEX_STATE_FILENAME)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _read_index_state(chroma_path: Path) -> dict[str, object]:
    path = chroma_path / INDEX_STATE_FILENAME
    if not path.exists():
        return {}
    try:
        root: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("memory index state is invalid") from None
    if not isinstance(root, dict) or set(root) != {"version", "characters"}:
        raise ValueError("memory index state root is invalid")
    if root["version"] != 1 or not isinstance(root["characters"], dict):
        raise ValueError("memory index state version is invalid")
    characters = root["characters"]
    if not all(isinstance(key, str) for key in characters):
        raise ValueError("memory index state character ids must be strings")
    return cast(dict[str, object], dict(characters))


def _flat_string_list(result: dict[str, object], field_name: str) -> list[str]:
    value = result.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Chroma result '{field_name}' must be a string list")
    return cast(list[str], value)


def _flat_object_list(
    result: dict[str, object], field_name: str
) -> list[dict[object, object]]:
    value = result.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Chroma result '{field_name}' must be an object list")
    return cast(list[dict[object, object]], value)


def _first_result_list(result: dict[str, object], field_name: str) -> list[object]:
    value = result.get(field_name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Chroma query result '{field_name}' must be a list")
    if not value:
        return []
    first_result = value[0]
    if not isinstance(first_result, list):
        raise ValueError(
            f"Chroma query result '{field_name}' must contain result lists"
        )
    return cast(list[object], first_result)


def _memory_search_candidate(
    memory_id: object,
    distance: object,
) -> MemorySearchCandidate:
    if not isinstance(memory_id, str):
        raise ValueError("Chroma memory ids must be strings")
    if (
        not isinstance(distance, (int, float))
        or isinstance(distance, bool)
        or not math.isfinite(distance)
        or distance < 0
    ):
        raise ValueError("Chroma memory distances must be non-negative finite numbers")
    return MemorySearchCandidate(
        memory_id=memory_id,
        raw_distance=float(distance),
    )

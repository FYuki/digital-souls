from __future__ import annotations

import hashlib
import importlib
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol, cast

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


@dataclass(frozen=True)
class MemorySearchCandidate:
    memory_id: str
    raw_distance: float


@dataclass(frozen=True)
class MemorySearchResult:
    memory_id: str
    normalized_text: str
    effective_at: str
    memory_type: str
    raw_distance: float


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
    effective_at: str,
    expires_at: str | None,
    chroma_path: Path,
) -> None:
    metadata = memory_index_metadata(
        character_id=character_id,
        provider_id=provider_id,
        memory_kind=memory_kind,
        memory_type=memory_type,
        policy_version=policy_version,
        effective_at=effective_at,
        expires_at=expires_at,
    )
    collection = _collection(character_id, chroma_path)
    # Chroma の版によらず、訂正前の metadata キーを残さないため置換する。
    collection.delete(ids=[memory_id])
    collection.upsert(
        ids=[memory_id],
        embeddings=[embedding],
        documents=[normalized_text],
        metadatas=[metadata],
    )


def delete_memory_index_entry(
    *, character_id: str, memory_id: str, chroma_path: Path
) -> None:
    _collection(character_id, chroma_path).delete(ids=[memory_id])


def list_memory_index_ids(*, character_id: str, chroma_path: Path) -> set[str]:
    result = _collection(character_id, chroma_path).get(include=[])
    return set(_flat_string_list(result, "ids"))


def get_memory_index_metadata(
    *, character_id: str, memory_id: str, chroma_path: Path
) -> dict[str, str] | None:
    result = _collection(character_id, chroma_path).get(ids=[memory_id])
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
) -> list[MemorySearchCandidate]:
    collection = _collection(character, chroma_path)
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
    if not ids:
        return []
    if len(ids) != len(distances):
        raise ValueError("Chroma query result ids and distances must match")
    return [
        _memory_search_candidate(memory_id, distance)
        for memory_id, distance in zip(ids, distances, strict=True)
    ]


def _collection(character: str, chroma_path: Path) -> _ChromaCollection:
    collection_name = _collection_name(character)
    chroma_path.mkdir(parents=True, exist_ok=True)
    return _client(str(chroma_path)).get_or_create_collection(name=collection_name)


@lru_cache(maxsize=None)
def _client(chroma_path: str) -> _ChromaClient:
    chromadb = importlib.import_module("chromadb")
    return cast(_ChromaClient, chromadb.PersistentClient(path=chroma_path))


def memory_index_metadata(
    *,
    character_id: str,
    provider_id: str,
    memory_kind: str,
    memory_type: str,
    policy_version: str,
    effective_at: str,
    expires_at: str | None,
) -> dict[str, str]:
    return {
        "character_id": character_id,
        "provider_id": provider_id,
        "memory_kind": memory_kind,
        "memory_type": memory_type,
        "policy_version": policy_version,
        "effective_at": effective_at,
        **({"expires_at": expires_at} if expires_at is not None else {}),
    }


def _collection_name(character: str) -> str:
    normalized = character.strip()
    if not normalized:
        raise ValueError("character must not be empty")
    slug = re.sub(r"[^a-z0-9_-]+", "-", normalized.lower()).strip("-_")
    slug = slug[:COLLECTION_NAME_MAX_SLUG_LENGTH].strip("-_")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[
        :COLLECTION_NAME_DIGEST_LENGTH
    ]
    return (
        f"{COLLECTION_NAME_PREFIX}-{slug}-{digest}"
        if slug
        else f"{COLLECTION_NAME_PREFIX}-{digest}"
    )


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
    ):
        raise ValueError("Chroma memory distances must be finite numbers")
    return MemorySearchCandidate(
        memory_id=memory_id,
        raw_distance=float(distance),
    )

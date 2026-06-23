import importlib
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CHROMA_PATH = DATA_DIR / "chroma"
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
class MemorySearchResult:
    content: str
    timestamp: str
    role: str


class ChromaCollection(Protocol):
    def add(
        self,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, str]],
    ) -> None:
        ...

    def query(
        self,
        *,
        query_embeddings: list[list[float]],
        n_results: int,
    ) -> dict[str, object]:
        ...


class ChromaClient(Protocol):
    def get_or_create_collection(self, name: str) -> ChromaCollection:
        ...


def resolve_collection_name(character: str) -> str:
    normalized = character.strip()
    if not normalized:
        raise ValueError("character must not be empty")
    slug_source = normalized.lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", slug_source).strip("-_")
    slug = slug[:COLLECTION_NAME_MAX_SLUG_LENGTH].strip("-_")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[
        :COLLECTION_NAME_DIGEST_LENGTH
    ]
    if slug:
        return f"{COLLECTION_NAME_PREFIX}-{slug}-{digest}"
    return f"{COLLECTION_NAME_PREFIX}-{digest}"


def _collection(character: str) -> ChromaCollection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    chromadb = importlib.import_module("chromadb")
    client = cast(ChromaClient, chromadb.PersistentClient(path=str(CHROMA_PATH)))
    return client.get_or_create_collection(name=resolve_collection_name(character))


def add_memory(
    character: str,
    record_id: int,
    embedding: list[float],
    content: str,
    metadata: dict[str, str],
) -> None:
    _collection(character).add(
        ids=[str(record_id)],
        embeddings=[embedding],
        documents=[content],
        metadatas=[metadata],
    )


def query_memories(
    character: str,
    embedding: list[float],
    n_results: int,
) -> list[MemorySearchResult]:
    result = _collection(character).query(
        query_embeddings=[embedding],
        n_results=n_results,
    )
    documents = _first_result_list(result, "documents")
    metadatas = _first_result_list(result, "metadatas")
    if not documents:
        return []
    if len(documents) != len(metadatas):
        raise ValueError("Chroma query result documents and metadatas must match")
    return [
        _memory_search_result(document, metadata)
        for document, metadata in zip(documents, metadatas, strict=True)
    ]


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


def _memory_search_result(
    document: object,
    metadata: object,
) -> MemorySearchResult:
    if not isinstance(document, str):
        raise ValueError("Chroma query result documents must be strings")
    if not isinstance(metadata, dict):
        raise ValueError("Chroma query result metadatas must be objects")
    timestamp = metadata.get("timestamp")
    role = metadata.get("role")
    if not isinstance(timestamp, str):
        raise ValueError("Chroma query result metadata.timestamp must be a string")
    if not isinstance(role, str):
        raise ValueError("Chroma query result metadata.role must be a string")
    return MemorySearchResult(content=document, timestamp=timestamp, role=role)

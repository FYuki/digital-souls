import importlib
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest


class FakeCollection:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.get_calls: list[dict[str, object]] = []
        self.query_calls: list[dict[str, object]] = []

    def upsert(self, **kwargs: object) -> None:
        ids = kwargs["ids"]
        documents = kwargs["documents"]
        embeddings = kwargs["embeddings"]
        metadatas = kwargs["metadatas"]
        assert isinstance(ids, list)
        assert isinstance(documents, list)
        assert isinstance(embeddings, list)
        assert isinstance(metadatas, list)
        for record_id, document, embedding, metadata in zip(
            ids, documents, embeddings, metadatas, strict=True
        ):
            assert isinstance(record_id, str)
            assert isinstance(metadata, dict)
            current = self.records.get(record_id, {})
            self.records[record_id] = {
                **current,
                "document": document,
                "embedding": embedding,
                "metadata": {**current.get("metadata", {}), **metadata},
            }

    def delete(self, *, ids: list[str]) -> None:
        for record_id in ids:
            self.records.pop(record_id, None)

    def count(self) -> int:
        return len(self.records)

    def get(self, **kwargs: object) -> dict[str, object]:
        self.get_calls.append(kwargs)
        ids_arg = kwargs.get("ids")
        matching_ids = (
            list(self.records)
            if ids_arg is None
            else [record_id for record_id in ids_arg if record_id in self.records]
        )
        offset = int(kwargs.get("offset", 0))
        limit_arg = kwargs.get("limit")
        ids = (
            matching_ids[offset:]
            if limit_arg is None
            else matching_ids[offset : offset + int(limit_arg)]
        )
        result: dict[str, object] = {"ids": ids}
        if kwargs.get("include") != []:
            result.update(
                {
                    "documents": [
                        self.records[record_id]["document"] for record_id in ids
                    ],
                    "metadatas": [
                        self.records[record_id]["metadata"] for record_id in ids
                    ],
                }
            )
        return result

    def query(self, **kwargs: object) -> dict[str, object]:
        self.query_calls.append(kwargs)
        ids = list(self.records)[: int(kwargs["n_results"])]
        return {
            "ids": [ids],
            "distances": [[1.25 + index for index, _record_id in enumerate(ids)]],
        }


class FakePersistentClient:
    collections_by_path: dict[str, dict[str, FakeCollection]] = {}
    instances: list["FakePersistentClient"] = []

    def __init__(self, path: str) -> None:
        self.path = path
        self.collections = self.collections_by_path.setdefault(path, {})
        self.instances.append(self)

    def get_or_create_collection(self, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection())

    def delete_collection(self, name: str) -> None:
        self.collections.pop(name, None)


def _import_chroma_store(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    fake_chromadb = ModuleType("chromadb")
    setattr(fake_chromadb, "PersistentClient", FakePersistentClient)
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    monkeypatch.delitem(sys.modules, "app.memory.chroma_store", raising=False)
    chroma_store = importlib.import_module("app.memory.chroma_store")
    FakePersistentClient.collections_by_path.clear()
    FakePersistentClient.instances.clear()
    return chroma_store


def _upsert(chroma_store: ModuleType, tmp_path: Path, **overrides: object) -> None:
    values: dict[str, object] = {
        "character_id": "miori",
        "memory_id": "00000000-0000-4000-8000-000000000042",
        "embedding": [0.1, 0.2],
        "normalized_text": "畑の相談",
        "provider_id": "core",
        "memory_kind": "SEMANTIC",
        "memory_type": "USER_PREFERENCE",
        "policy_version": "policy-v1",
        "effective_at": "2026-06-23T00:00:00.000000Z",
        "expires_at": None,
        "chroma_path": tmp_path / "data" / "chroma",
    }
    values.update(overrides)
    chroma_store.upsert_memory_index_entry(**values)


def _only_collection() -> FakeCollection:
    collections = next(iter(FakePersistentClient.collections_by_path.values()))
    return next(iter(collections.values()))


def test_upsert_exactly_replaces_record_with_approved_memory_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)
    _upsert(chroma_store, tmp_path)
    collection = _only_collection()
    record_id = "00000000-0000-4000-8000-000000000042"
    collection.records[record_id]["metadata"] = {
        **collection.records[record_id]["metadata"],
        "role": "user",
        "timestamp": "legacy",
        "last_user_mentioned_at": "private-marker",
    }

    _upsert(chroma_store, tmp_path, normalized_text="訂正後の畑の相談")

    assert collection.records == {
        record_id: {
            "document": "訂正後の畑の相談",
            "embedding": [0.1, 0.2],
            "metadata": {
                "character_id": "miori",
                "provider_id": "core",
                "memory_kind": "SEMANTIC",
                "memory_type": "USER_PREFERENCE",
                "policy_version": "policy-v1",
                "effective_at": "2026-06-23T00:00:00.000000Z",
            },
        }
    }


def test_upsert_includes_expires_at_only_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)

    _upsert(
        chroma_store,
        tmp_path,
        expires_at="2026-07-23T00:00:00.000000Z",
    )

    assert _only_collection().records["00000000-0000-4000-8000-000000000042"][
        "metadata"
    ] == {
        "character_id": "miori",
        "provider_id": "core",
        "memory_kind": "SEMANTIC",
        "memory_type": "USER_PREFERENCE",
        "policy_version": "policy-v1",
        "effective_at": "2026-06-23T00:00:00.000000Z",
        "expires_at": "2026-07-23T00:00:00.000000Z",
    }


def test_delete_is_idempotent_and_character_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)
    memory_id = "00000000-0000-4000-8000-000000000042"
    _upsert(chroma_store, tmp_path)
    _upsert(chroma_store, tmp_path, character_id="other")

    chroma_store.delete_memory_index_entry(
        character_id="miori",
        memory_id=memory_id,
        chroma_path=tmp_path / "data" / "chroma",
    )
    chroma_store.delete_memory_index_entry(
        character_id="miori",
        memory_id=memory_id,
        chroma_path=tmp_path / "data" / "chroma",
    )

    assert (
        chroma_store.list_memory_index_ids(
            character_id="miori", chroma_path=tmp_path / "data" / "chroma"
        )
        == set()
    )
    assert chroma_store.list_memory_index_ids(
        character_id="other", chroma_path=tmp_path / "data" / "chroma"
    ) == {memory_id}


def test_list_ids_and_get_metadata_observe_the_character_collection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)
    memory_id = "00000000-0000-4000-8000-000000000042"
    _upsert(chroma_store, tmp_path)

    ids = chroma_store.list_memory_index_ids(
        character_id="miori", chroma_path=tmp_path / "data" / "chroma"
    )
    metadata = chroma_store.get_memory_index_metadata(
        character_id="miori",
        memory_id=memory_id,
        chroma_path=tmp_path / "data" / "chroma",
    )

    assert ids == {memory_id}
    assert metadata == _only_collection().records[memory_id]["metadata"]
    assert (
        chroma_store.get_memory_index_metadata(
            character_id="miori",
            memory_id="missing",
            chroma_path=tmp_path / "data" / "chroma",
        )
        is None
    )


def test_list_ids_returns_all_ids_without_loading_payloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)
    expected_ids = {f"memory-{index:03d}" for index in range(125)}
    for memory_id in expected_ids:
        _upsert(chroma_store, tmp_path, memory_id=memory_id)

    ids = chroma_store.list_memory_index_ids(
        character_id="miori", chroma_path=tmp_path / "data" / "chroma"
    )

    assert ids == expected_ids
    assert _only_collection().get_calls == [{"include": []}]


def test_persistent_client_is_reused_for_the_same_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)

    _upsert(chroma_store, tmp_path)
    chroma_store.query_memories(
        "miori", [0.3, 0.4], n_results=5, chroma_path=tmp_path / "data" / "chroma"
    )

    assert len(FakePersistentClient.instances) == 1


def test_delete_collection_removes_the_character_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)
    chroma_path = tmp_path / "data" / "chroma"
    _upsert(chroma_store, tmp_path)

    chroma_store.delete_memory_index_collection(
        character_id="miori", chroma_path=chroma_path
    )

    assert FakePersistentClient.collections_by_path[str(chroma_path)] == {}

    chroma_store.delete_memory_index_collection(
        character_id="miori", chroma_path=chroma_path
    )


def test_delete_collection_preserves_non_missing_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)
    _upsert(chroma_store, tmp_path)
    client = FakePersistentClient.instances[0]
    monkeypatch.setattr(
        client,
        "delete_collection",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("delete failed")),
    )

    with pytest.raises(RuntimeError, match="delete failed"):
        chroma_store.delete_memory_index_collection(
            character_id="miori", chroma_path=tmp_path / "data" / "chroma"
        )


def test_query_returns_only_memory_id_and_unchanged_raw_distance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)
    _upsert(chroma_store, tmp_path)

    memories = chroma_store.query_memories(
        "miori", [0.3, 0.4], n_results=5, chroma_path=tmp_path / "data" / "chroma"
    )

    assert memories == [
        chroma_store.MemorySearchCandidate(
            memory_id="00000000-0000-4000-8000-000000000042",
            raw_distance=1.25,
        )
    ]
    assert _only_collection().query_calls == [
        {
            "query_embeddings": [[0.3, 0.4]],
            "n_results": 5,
            "include": ["distances"],
        }
    ]


def test_query_preserves_chroma_order_without_threshold_or_tie_break(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)
    _upsert(chroma_store, tmp_path, memory_id="farther")
    _upsert(chroma_store, tmp_path, memory_id="nearer")
    collection = _only_collection()

    def fake_query(**kwargs: object) -> dict[str, object]:
        collection.query_calls.append(kwargs)
        return {"ids": [["farther", "nearer"]], "distances": [[1.75, 0.25]]}

    monkeypatch.setattr(collection, "query", fake_query)

    memories = chroma_store.query_memories(
        "miori", [0.3, 0.4], n_results=2, chroma_path=tmp_path / "data" / "chroma"
    )

    assert [(item.memory_id, item.raw_distance) for item in memories] == [
        ("farther", 1.75),
        ("nearer", 0.25),
    ]
    assert len(collection.query_calls) == 1


@pytest.mark.parametrize(
    "response",
    (
        {"ids": [[]], "distances": [[0.25]]},
        {"ids": [["memory-1"]], "distances": [[]]},
        {"ids": [["memory-1"]], "distances": [["not-a-number"]]},
        {"ids": [["memory-1"]], "distances": [[float("inf")]]},
        {"ids": [["memory-1"]], "distances": [[-0.01]]},
    ),
)
def test_query_rejects_mismatched_or_invalid_distances(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response: dict[str, object],
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)
    _upsert(chroma_store, tmp_path)
    monkeypatch.setattr(_only_collection(), "query", lambda **_kwargs: response)

    with pytest.raises(ValueError):
        chroma_store.query_memories(
            "miori", [0.3, 0.4], n_results=5, chroma_path=tmp_path / "data" / "chroma"
        )


def test_character_names_remain_safe_and_isolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)
    _upsert(chroma_store, tmp_path, character_id="Miori")
    _upsert(chroma_store, tmp_path, character_id="miori")
    _upsert(chroma_store, tmp_path, character_id="光織/mi")

    names = list(next(iter(FakePersistentClient.collections_by_path.values())))
    assert len(set(names)) == 3
    assert all(_is_chroma_safe_name(name) for name in names)


def test_chroma_path_is_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chroma_store = _import_chroma_store(monkeypatch)

    with pytest.raises(TypeError):
        chroma_store.list_memory_index_ids(character_id="miori")

    assert FakePersistentClient.instances == []


def _is_chroma_safe_name(collection_name: str) -> bool:
    return (
        3 <= len(collection_name) <= 63
        and re.fullmatch(r"[a-z0-9][a-z0-9_-]*[a-z0-9]", collection_name) is not None
    )

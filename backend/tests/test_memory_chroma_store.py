import importlib
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest


class FakeCollection:
    def __init__(self) -> None:
        self.add_calls: list[dict[str, object]] = []
        self.query_calls: list[dict[str, object]] = []
        self.query_result: dict[str, list[list[object]]] = {
            "documents": [["雨の話", "畑の話"]],
            "metadatas": [
                [
                    {"role": "user", "timestamp": "2026-06-20T00:00:00+00:00"},
                    {"role": "assistant", "timestamp": "2026-06-21T00:00:00+00:00"},
                ]
            ],
        }

    def add(self, **kwargs: object) -> None:
        self.add_calls.append(kwargs)

    def query(self, **kwargs: object) -> dict[str, list[list[object]]]:
        self.query_calls.append(kwargs)
        return self.query_result


class FakePersistentClient:
    instances: list["FakePersistentClient"] = []

    def __init__(self, path: str) -> None:
        self.path = path
        self.collections: dict[str, FakeCollection] = {}
        FakePersistentClient.instances.append(self)

    def get_or_create_collection(self, name: str) -> FakeCollection:
        collection = self.collections.setdefault(name, FakeCollection())
        return collection


def _import_chroma_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> ModuleType:
    fake_chromadb = ModuleType("chromadb")
    setattr(fake_chromadb, "PersistentClient", FakePersistentClient)
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    sys.modules.pop("app.memory.chroma_store", None)

    chroma_store = importlib.import_module("app.memory.chroma_store")
    module = importlib.reload(chroma_store)
    monkeypatch.setattr(module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(module, "CHROMA_PATH", tmp_path / "data" / "chroma")
    FakePersistentClient.instances.clear()
    return module


class TestChromaStore:
    def test_add_memory_uses_safe_character_collection_and_required_payload(
        self, monkeypatch, tmp_path
    ):
        chroma_store = _import_chroma_store(monkeypatch, tmp_path)
        metadata = {
            "character": "miori",
            "role": "user",
            "timestamp": "2026-06-23T00:00:00+00:00",
        }

        chroma_store.add_memory("miori", 42, [0.1, 0.2], "畑の相談", metadata)

        client = FakePersistentClient.instances[0]
        collection_name = next(iter(client.collections))
        collection = client.collections[collection_name]
        assert client.path == str(tmp_path / "data" / "chroma")
        assert collection_name.startswith("character-miori-")
        assert collection.add_calls == [
            {
                "ids": ["42"],
                "embeddings": [[0.1, 0.2]],
                "documents": ["畑の相談"],
                "metadatas": [metadata],
            }
        ]

    def test_query_memories_returns_timestamped_results_from_character_collection(
        self, monkeypatch, tmp_path
    ):
        chroma_store = _import_chroma_store(monkeypatch, tmp_path)

        memories = chroma_store.query_memories("miori", [0.3, 0.4], n_results=5)

        client = FakePersistentClient.instances[0]
        collection_name = next(iter(client.collections))
        collection = client.collections[collection_name]
        assert collection_name.startswith("character-miori-")
        assert memories == [
            chroma_store.MemorySearchResult(
                content="雨の話",
                timestamp="2026-06-20T00:00:00+00:00",
                role="user",
            ),
            chroma_store.MemorySearchResult(
                content="畑の話",
                timestamp="2026-06-21T00:00:00+00:00",
                role="assistant",
            ),
        ]
        assert collection.query_calls == [
            {"query_embeddings": [[0.3, 0.4]], "n_results": 5}
        ]

    def test_short_and_symbol_character_names_use_chroma_safe_collections(
        self, monkeypatch, tmp_path
    ):
        chroma_store = _import_chroma_store(monkeypatch, tmp_path)

        chroma_store.add_memory(
            "mi",
            43,
            [0.1, 0.2],
            "短いキャラクター名の記憶",
            {
                "character": "mi",
                "role": "user",
                "timestamp": "2026-06-23T00:00:00+00:00",
            },
        )
        chroma_store.query_memories("光織/mi", [0.3, 0.4], n_results=5)

        add_client = FakePersistentClient.instances[0]
        query_client = FakePersistentClient.instances[1]
        add_name = next(iter(add_client.collections))
        query_name = next(iter(query_client.collections))
        assert add_name.startswith("character-mi-")
        assert query_name.startswith("character-mi-")
        assert add_name != query_name
        assert _is_chroma_safe_name(add_name)
        assert _is_chroma_safe_name(query_name)
        assert add_client.collections[add_name].add_calls[0]["ids"] == ["43"]
        assert query_client.collections[query_name].query_calls == [
            {"query_embeddings": [[0.3, 0.4]], "n_results": 5}
        ]

    def test_case_variant_character_names_keep_separate_collection_boundaries(
        self, monkeypatch, tmp_path
    ):
        chroma_store = _import_chroma_store(monkeypatch, tmp_path)

        chroma_store.add_memory(
            "Miori",
            44,
            [0.1, 0.2],
            "別キャラクターの記憶",
            {
                "character": "Miori",
                "role": "user",
                "timestamp": "2026-06-23T00:00:00+00:00",
            },
        )
        chroma_store.query_memories("miori", [0.3, 0.4], n_results=5)

        add_client = FakePersistentClient.instances[0]
        query_client = FakePersistentClient.instances[1]
        add_name = next(iter(add_client.collections))
        query_name = next(iter(query_client.collections))
        assert add_name != query_name
        assert add_client.collections[add_name].add_calls[0]["ids"] == ["44"]
        assert query_client.collections[query_name].query_calls == [
            {"query_embeddings": [[0.3, 0.4]], "n_results": 5}
        ]

    def test_long_character_name_stays_chroma_safe_for_add_and_query(
        self, monkeypatch, tmp_path
    ):
        chroma_store = _import_chroma_store(monkeypatch, tmp_path)
        character = "a" * 80

        chroma_store.add_memory(
            character,
            45,
            [0.1, 0.2],
            "長いキャラクター名の記憶",
            {
                "character": character,
                "role": "user",
                "timestamp": "2026-06-23T00:00:00+00:00",
            },
        )
        memories = chroma_store.query_memories(character, [0.3, 0.4], n_results=5)

        add_client = FakePersistentClient.instances[0]
        query_client = FakePersistentClient.instances[1]
        add_name = next(iter(add_client.collections))
        query_name = next(iter(query_client.collections))
        assert add_name == query_name
        assert _is_chroma_safe_name(add_name)
        assert add_client.collections[add_name].add_calls[0]["ids"] == ["45"]
        assert query_client.collections[query_name].query_calls == [
            {"query_embeddings": [[0.3, 0.4]], "n_results": 5}
        ]
        assert memories[0].content == "雨の話"

    def test_empty_character_name_is_rejected_before_client_access(
        self, monkeypatch, tmp_path
    ):
        chroma_store = _import_chroma_store(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="character must not be empty"):
            chroma_store.query_memories(" ", [0.3, 0.4], n_results=5)

        assert FakePersistentClient.instances == []


def _is_chroma_safe_name(collection_name: str) -> bool:
    return (
        3 <= len(collection_name) <= 63
        and re.fullmatch(r"[a-z0-9][a-z0-9_-]*[a-z0-9]", collection_name) is not None
    )

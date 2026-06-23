import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest


class FakeCollection:
    def __init__(self) -> None:
        self.add_calls = []
        self.query_calls = []
        self.query_result = {
            "documents": [["雨の話", "畑の話"]],
            "metadatas": [
                [
                    {"role": "user", "timestamp": "2026-06-20T00:00:00+00:00"},
                    {"role": "assistant", "timestamp": "2026-06-21T00:00:00+00:00"},
                ]
            ],
        }

    def add(self, **kwargs):
        self.add_calls.append(kwargs)

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return self.query_result


class FakePersistentClient:
    instances = []

    def __init__(self, path: str) -> None:
        self.path = path
        self.collections = {}
        FakePersistentClient.instances.append(self)

    def get_or_create_collection(self, name: str):
        collection = self.collections.setdefault(name, FakeCollection())
        return collection


def _import_chroma_store(monkeypatch, tmp_path: Path):
    fake_chromadb = ModuleType("chromadb")
    fake_chromadb.PersistentClient = FakePersistentClient
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
        collection_name = chroma_store.resolve_collection_name("miori")
        collection = client.collections[collection_name]
        assert client.path == str(tmp_path / "data" / "chroma")
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
        collection_name = chroma_store.resolve_collection_name("miori")
        collection = client.collections[collection_name]
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

    def test_resolve_collection_name_accepts_short_character_names(
        self, monkeypatch, tmp_path
    ):
        chroma_store = _import_chroma_store(monkeypatch, tmp_path)

        collection_name = chroma_store.resolve_collection_name("mi")

        assert collection_name.startswith("character-mi-")
        assert len(collection_name) >= 3
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
        client = FakePersistentClient.instances[0]
        assert collection_name in client.collections

    def test_resolve_collection_name_is_stable_for_symbols(
        self, monkeypatch, tmp_path
    ):
        chroma_store = _import_chroma_store(monkeypatch, tmp_path)

        first_name = chroma_store.resolve_collection_name("光織/mi")
        second_name = chroma_store.resolve_collection_name("光織/mi")

        assert first_name == second_name
        assert first_name.startswith("character-mi-")

    def test_resolve_collection_name_preserves_case_sensitive_character_boundary(
        self, monkeypatch, tmp_path
    ):
        chroma_store = _import_chroma_store(monkeypatch, tmp_path)

        lower_name = chroma_store.resolve_collection_name("miori")
        upper_name = chroma_store.resolve_collection_name("Miori")

        assert lower_name != upper_name

    def test_long_character_name_stays_chroma_safe_for_add_and_query(
        self, monkeypatch, tmp_path
    ):
        chroma_store = _import_chroma_store(monkeypatch, tmp_path)
        character = "a" * 80

        collection_name = chroma_store.resolve_collection_name(character)
        chroma_store.add_memory(
            character,
            44,
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
        add_collection = add_client.collections[collection_name]
        query_collection = query_client.collections[collection_name]
        assert 3 <= len(collection_name) <= 63
        assert collection_name in add_client.collections
        assert collection_name in query_client.collections
        assert add_collection.add_calls[0]["ids"] == ["44"]
        assert query_collection.query_calls == [
            {"query_embeddings": [[0.3, 0.4]], "n_results": 5}
        ]
        assert memories[0].content == "雨の話"

    def test_resolve_collection_name_rejects_empty_character(
        self, monkeypatch, tmp_path
    ):
        chroma_store = _import_chroma_store(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="character must not be empty"):
            chroma_store.resolve_collection_name(" ")

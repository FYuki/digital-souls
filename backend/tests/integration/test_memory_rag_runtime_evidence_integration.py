import importlib
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from tests.character_card_test_support import (
    character_card_data,
    character_card_document,
    write_character_card,
)


def _require_runtime_evidence_dependencies() -> None:
    importlib.import_module("chromadb")

    from app.llm.ollama_config import (
        resolve_ollama_base_url,
        resolve_ollama_embedding_model,
    )

    response = httpx.get(f"{resolve_ollama_base_url()}/api/tags", timeout=5.0)
    response.raise_for_status()

    models = response.json().get("models")
    if not isinstance(models, list):
        pytest.fail("Ollama tags response does not include models")
    model_name = resolve_ollama_embedding_model()
    available = {
        model.get("name")
        for model in models
        if isinstance(model, dict) and isinstance(model.get("name"), str)
    }
    if model_name not in available:
        pytest.fail(f"Ollama model is not pulled: {model_name}")


def _load_runtime_modules() -> dict[str, object]:
    module_names = (
        "app.memory.chroma_store",
        "app.memory.rag_service",
        "app._chat_runtime",
        "app.chat_service",
        "app.routers.chat",
        "app.main",
    )
    return {
        module_name: importlib.import_module(module_name) for module_name in module_names
    }


def _isolate_memory_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.memory.chroma_store as chroma_store
    import app.memory.rag_service as rag_service

    data_dir = tmp_path / "data"
    monkeypatch.setattr(chroma_store, "DATA_DIR", data_dir)
    monkeypatch.setattr(chroma_store, "CHROMA_PATH", data_dir / "chroma")
    monkeypatch.setattr(rag_service, "add_memory", chroma_store.add_memory)
    monkeypatch.setattr(rag_service, "query_memories", chroma_store.query_memories)


def _write_character(tmp_path: Path, character: str, system_prompt: str) -> None:
    data = character_card_data(
        description="",
        personality="",
        scenario="",
        system_prompt=system_prompt,
        mes_example="",
        post_history_instructions="",
    )
    write_character_card(
        tmp_path,
        character,
        character_card_document(data=data),
    )


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


class TestRagRuntimeEvidenceIntegration:
    def test_real_chat_store_chroma_query_and_prompt_injection_reach_llm(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text:latest")
        _require_runtime_evidence_dependencies()
        modules = _load_runtime_modules()
        _isolate_memory_paths(tmp_path, monkeypatch)

        import app.characters.loader as loader_module
        from app.memory.embedder import embed_text

        chroma_store = modules["app.memory.chroma_store"]
        main = modules["app.main"]
        app = main.app
        character = f"miori{uuid4().hex[:8]}"
        system_prompt = "# 光織\nあなたは光織です。"
        stored_memory = "農業日誌: 保存して。2026-06-23はトマト畑に水やりした"
        conversation_id = str(uuid4())
        _write_character(tmp_path, character, system_prompt)
        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: tmp_path)
        monkeypatch.setenv("RAG_ENABLED", "true")

        captured_llm_calls = []

        def capture_generate_response(
            prompt, *, max_output_tokens: int, settings
        ) -> str:
            assert max_output_tokens == 1024
            assert settings.assistant_max_generation_tokens == 1024
            messages = prompt.messages
            user_message = next(
                message.content
                for message in reversed(messages)
                if message.role.value == "user"
            )
            captured_llm_calls.append(prompt)
            if user_message == stored_memory:
                return "農業日誌として保存しました。"
            return "前回はトマト畑に水やりしました。"

        monkeypatch.setattr(
            main.llm_router,
            "generate_response",
            capture_generate_response,
        )

        with TestClient(app) as client:
            save_response = client.post(
                "/chat",
                json={
                    "character": character,
                    "conversation_id": conversation_id,
                    "message": stored_memory,
                },
            )
            assert save_response.status_code == 200
            assert save_response.json()["response"] == "農業日誌として保存しました。"

            query_embedding = embed_text("前回の畑作業は?")
            query_results = []

            def memory_was_persisted() -> bool:
                nonlocal query_results
                query_results = chroma_store.query_memories(character, query_embedding, 5)
                return any(result.content == stored_memory for result in query_results)

            _wait_until(memory_was_persisted)
            assert any(result.content == stored_memory for result in query_results)

            response = client.post(
                "/chat",
                json={
                    "character": character,
                    "conversation_id": conversation_id,
                    "message": "前回の畑作業は?",
                },
            )

        assert response.status_code == 200
        assert response.json()["response"] == "前回はトマト畑に水やりしました。"
        contents = [
            message.content
            for message in captured_llm_calls[-1].messages
        ]
        assert contents[-1] == "前回の畑作業は?"
        assert any("関連する記憶" in content for content in contents)
        assert any(stored_memory in content for content in contents)

    def test_real_storage_failure_chat_continues_without_failed_memory_file(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text:latest")
        _require_runtime_evidence_dependencies()
        modules = _load_runtime_modules()
        _isolate_memory_paths(tmp_path, monkeypatch)

        import app.characters.loader as loader_module
        import chromadb

        rag_service = modules["app.memory.rag_service"]
        main = modules["app.main"]
        app = main.app
        system_prompt = "# 光織\nあなたは光織です。"
        user_message = "農業日誌: 保存して。2026-06-23はナスに追肥した"
        conversation_id = str(uuid4())
        _write_character(tmp_path, "miori", system_prompt)
        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: tmp_path)
        monkeypatch.setenv("RAG_ENABLED", "true")

        original_persistent_client = chromadb.PersistentClient

        class AddFailureCollection:
            def __init__(self, collection):
                self.collection = collection

            def add(self, **kwargs):
                raise RuntimeError("injected chroma add failure")

            def query(self, **kwargs):
                return self.collection.query(**kwargs)

        class AddFailureClient:
            def __init__(self, path: str):
                self.client = original_persistent_client(path=path)

            def get_or_create_collection(self, name: str):
                collection = self.client.get_or_create_collection(name=name)
                return AddFailureCollection(collection)

        monkeypatch.setattr(chromadb, "PersistentClient", AddFailureClient)

        def capture_generate_response(
            prompt, *, max_output_tokens: int, settings
        ) -> str:
            assert max_output_tokens == 1024
            assert settings.assistant_max_generation_tokens == 1024
            contents = [message.content for message in prompt.messages]
            assert system_prompt in contents[0]
            assert contents[-1] == user_message
            return "農業日誌として保存しました。"

        monkeypatch.setattr(
            main.llm_router,
            "generate_response",
            capture_generate_response,
        )

        with TestClient(app) as client:
            response = client.post(
                "/chat",
                json={
                    "character": "miori",
                    "conversation_id": conversation_id,
                    "message": user_message,
                },
            )

        assert response.status_code == 200
        assert response.json() == {
            "character": "miori",
            "response": "農業日誌として保存しました。",
        }

        assert not tmp_path.joinpath("data", "failed-memories.jsonl").exists()

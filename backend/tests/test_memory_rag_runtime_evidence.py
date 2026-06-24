import importlib
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from fastapi.testclient import TestClient


def _require_runtime_evidence_dependencies() -> None:
    try:
        importlib.import_module("chromadb")
    except ModuleNotFoundError:
        pytest.skip("chromadb is not installed")

    from app.llm.ollama_config import (
        resolve_ollama_base_url,
        resolve_ollama_embedding_model,
    )

    try:
        response = httpx.get(f"{resolve_ollama_base_url()}/api/tags", timeout=5.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"Ollama is not available: {exc.__class__.__name__}")

    models = response.json().get("models")
    if not isinstance(models, list):
        pytest.skip("Ollama tags response does not include models")
    model_name = resolve_ollama_embedding_model()
    available = {
        model.get("name")
        for model in models
        if isinstance(model, dict) and isinstance(model.get("name"), str)
    }
    if model_name not in available:
        pytest.skip(f"Ollama model is not pulled: {model_name}")


def _load_runtime_modules() -> dict[str, object]:
    module_names = (
        "app.memory.chroma_store",
        "app.memory.conversation_log",
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
    import app.memory.conversation_log as conversation_log
    import app.memory.rag_service as rag_service

    data_dir = tmp_path / "data"
    monkeypatch.setattr(chroma_store, "DATA_DIR", data_dir)
    monkeypatch.setattr(chroma_store, "CHROMA_PATH", data_dir / "chroma")
    monkeypatch.setattr(conversation_log, "DATA_DIR", data_dir)
    monkeypatch.setattr(conversation_log, "DB_PATH", data_dir / "conversations.db")
    monkeypatch.setattr(rag_service, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        rag_service,
        "FAILED_MEMORY_LOG_PATH",
        data_dir / "failed-memories.jsonl",
    )
    monkeypatch.setattr(rag_service, "add_memory", chroma_store.add_memory)
    monkeypatch.setattr(rag_service, "query_memories", chroma_store.query_memories)


def _write_character(tmp_path: Path, character: str, system_prompt: str) -> None:
    character_dir = tmp_path / "characters" / character
    character_dir.mkdir(parents=True)
    character_dir.joinpath("personality.md").write_text(system_prompt, encoding="utf-8")


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


class TestRagRuntimeEvidence:
    def test_runtime_module_setup_keeps_websocket_exception_bindings_current(self):
        import app.routers.ws as ws_router

        modules = _load_runtime_modules()
        chat_service = modules["app.chat_service"]

        assert ws_router.CharacterNotFoundError is chat_service.CharacterNotFoundError
        assert ws_router.ChatBackendError is chat_service.ChatBackendError
        assert ws_router.ChatTimeoutError is chat_service.ChatTimeoutError

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
        chat_runtime = modules["app._chat_runtime"]
        app = modules["app.main"].app
        character = f"miori{uuid4().hex[:8]}"
        system_prompt = "# 光織\nあなたは光織です。"
        stored_memory = "農業日誌: 保存して。2026-06-23はトマト畑に水やりした"
        _write_character(tmp_path, character, system_prompt)
        monkeypatch.setattr(loader_module, "_get_repo_root", lambda: tmp_path)
        monkeypatch.setenv("RAG_ENABLED", "true")

        captured_llm_calls = []

        def capture_generate_response(system_prompt_arg: str, user_message: str) -> str:
            captured_llm_calls.append(
                {"system_prompt": system_prompt_arg, "user_message": user_message}
            )
            if user_message == stored_memory:
                return "農業日誌として保存しました。"
            return "前回はトマト畑に水やりしました。"

        monkeypatch.setattr(
            chat_runtime._llm_router,
            "generate_response",
            capture_generate_response,
        )

        with TestClient(app) as client:
            save_response = client.post(
                "/chat",
                json={"character": character, "message": stored_memory},
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
                json={"character": character, "message": "前回の畑作業は?"},
            )

        assert response.status_code == 200
        assert response.json()["response"] == "前回はトマト畑に水やりしました。"
        assert captured_llm_calls[-1]["user_message"] == "前回の畑作業は?"
        assert "過去の記憶:" in captured_llm_calls[-1]["system_prompt"]
        assert stored_memory in captured_llm_calls[-1]["system_prompt"]

    def test_real_storage_failure_chat_continues_and_failed_memory_is_written(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text:latest")
        _require_runtime_evidence_dependencies()
        modules = _load_runtime_modules()
        _isolate_memory_paths(tmp_path, monkeypatch)

        import app.characters.loader as loader_module
        import chromadb

        rag_service = modules["app.memory.rag_service"]
        chat_runtime = modules["app._chat_runtime"]
        app = modules["app.main"].app
        system_prompt = "# 光織\nあなたは光織です。"
        user_message = "農業日誌: 保存して。2026-06-23はナスに追肥した"
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

        def capture_generate_response(system_prompt_arg: str, user_message_arg: str) -> str:
            assert system_prompt_arg == system_prompt
            assert user_message_arg == user_message
            return "農業日誌として保存しました。"

        monkeypatch.setattr(
            chat_runtime._llm_router,
            "generate_response",
            capture_generate_response,
        )

        with TestClient(app) as client:
            response = client.post(
                "/chat",
                json={"character": "miori", "message": user_message},
            )

        assert response.status_code == 200
        assert response.json() == {
            "character": "miori",
            "response": "農業日誌として保存しました。",
        }

        _wait_until(lambda: rag_service.FAILED_MEMORY_LOG_PATH.exists())
        failed_lines = rag_service.FAILED_MEMORY_LOG_PATH.read_text(
            encoding="utf-8"
        ).splitlines()
        failed_payloads = [json.loads(line) for line in failed_lines]
        assert any(
            payload["character"] == "miori"
            and payload["role"] == "user"
            and payload["content"] == user_message
            for payload in failed_payloads
        )

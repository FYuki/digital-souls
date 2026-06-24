import inspect
import re
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).parent.parent


def _passed_result_counts_by_command(markdown: str) -> dict[str, str]:
    matches = re.findall(
        r"- Command: `([^`]+)`\n- Result: `([^`]*?\d+ passed[^`]*)`",
        markdown,
    )

    results: dict[str, str] = {}
    for command, result in matches:
        count = re.search(r"\d+ passed", result)
        if count is None:
            continue
        existing = results.get(command)
        if existing is not None and existing != count.group(0):
            raise AssertionError(f"Conflicting result counts for {command}")
        results[command] = count.group(0)
    return results


def _required_result_count(markdown: str, command: str) -> str:
    results = _passed_result_counts_by_command(markdown)
    if command not in results:
        raise AssertionError(f"Missing result evidence for {command}")
    return results[command]


def _read_required_text(path: Path) -> str:
    assert path.exists(), f"Missing required evidence file: {path}"
    return path.read_text(encoding="utf-8")


class TestRuntimeConfiguration:
    def test_runtime_requirements_include_fastapi_backend_dependencies(self):
        required = {"fastapi", "uvicorn[standard]", "httpx", "python-dotenv", "chromadb"}

        lines = (_BACKEND_DIR / "requirements.txt").read_text().splitlines()
        packages = {
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        }

        assert required.issubset(packages)

    def test_env_example_declares_ollama_base_url(self):
        lines = (_BACKEND_DIR / ".env.example").read_text().splitlines()

        assert "OLLAMA_BASE_URL=http://localhost:11434" in lines

    def test_env_example_declares_ollama_embedding_model(self):
        lines = (_BACKEND_DIR / ".env.example").read_text().splitlines()

        assert "OLLAMA_EMBEDDING_MODEL=nomic-embed-text:latest" in lines

    def test_env_example_declares_rag_enabled_switch(self):
        lines = (_BACKEND_DIR / ".env.example").read_text().splitlines()

        assert "RAG_ENABLED=false" in lines

    def test_rag_enabled_is_not_resolved_inside_memory_service(self):
        import app.memory.rag_service as rag_service

        source = inspect.getsource(rag_service)

        assert "os.environ" not in source
        assert "RAG_ENABLED" not in source

    def test_memory_policy_is_resolved_at_chat_boundary_only(self):
        import app.memory.rag_service as rag_service
        import app.chat_service as chat_service

        rag_source = inspect.getsource(rag_service)
        chat_service_source = inspect.getsource(chat_service)

        assert "resolved_memory_policy" not in rag_source
        assert "resolved_memory_policy" in chat_service_source

    def test_ollama_environment_is_resolved_in_llm_boundary_only(self):
        import app.memory.embedder as embedder

        source = inspect.getsource(embedder)

        assert "os.environ" not in source
        assert "OLLAMA_BASE_URL" not in source
        assert "OLLAMA_EMBEDDING_MODEL" not in source

    def test_memory_policy_config_declares_common_and_service_sections(self):
        import json

        config_path = _BACKEND_DIR / "app" / "memory" / "memory_policy.json"

        config = json.loads(config_path.read_text(encoding="utf-8"))

        assert set(config) == {"common", "services"}
        assert set(config["common"]) == {
            "sensitive_terms",
            "do_not_store_terms",
            "explicit_memory_terms",
            "long_term_memory_markers",
        }
        assert isinstance(config["services"], dict)
        assert "rag_service" in config["services"]
        assert isinstance(config["services"]["rag_service"], dict)
        assert "max_retrieved_memories" in config["services"]["rag_service"]
        assert "characters" not in config

    def test_memory_policy_module_does_not_expose_test_only_persistence_helper(self):
        import app.memory.memory_policy as memory_policy

        assert not hasattr(memory_policy, "can_persist_memory")

    def test_memory_modules_do_not_reference_character_memory_policy_markdown(self):
        memory_dir = _BACKEND_DIR / "app" / "memory"

        sources = [
            path.read_text(encoding="utf-8")
            for path in memory_dir.glob("*.py")
            if path.name != "__init__.py"
        ]

        assert all("memory-policy.md" not in source for source in sources)

    def test_test_report_uses_current_runtime_evidence_names(self):
        report = (_BACKEND_DIR.parent / "test-report.md").read_text(encoding="utf-8")

        assert (
            "TestChatEndpoint.test_rag_disabled_does_not_resolve_memory_policy_or_record"
            in report
        )
        assert (
            "TestEmbedder.test_embed_text_uses_embedding_model_environment_override"
            in report
        )
        assert (
            "test_build_augmented_system_prompt_returns_original_when_disabled"
            not in report
        )
        assert "test_record_chat_turn_does_not_write_when_disabled" not in report
        assert "test_embed_text_uses_embedding_model_from_environment" not in report
        assert "TestChatTaskQueueContract" not in report
        assert "test_thread_pool_task_queue_does_not_run_task_inline" not in report
        assert (
            "TestChatServiceErrorContract."
            "test_create_chat_session_does_not_require_event_loop_argument"
            in report
        )
        assert "Success: no issues found" in report
        assert "source files" not in report

    def test_test_report_passed_counts_match_fix_evidence(self):
        root = _BACKEND_DIR.parent
        fix_evidence = _read_required_text(root / "fix-evidence.md")
        report = _read_required_text(root / "test-report.md")

        fix_counts = _passed_result_counts_by_command(fix_evidence)
        report_counts = _passed_result_counts_by_command(report)

        assert fix_counts
        assert report_counts
        for command, report_count in report_counts.items():
            assert command in fix_counts
            assert fix_counts[command] == report_count

    def test_runtime_contract_full_suite_count_comes_from_fix_evidence(self):
        root = _BACKEND_DIR.parent
        fix_evidence = _read_required_text(root / "fix-evidence.md")
        report = _read_required_text(root / "test-report.md")

        full_suite_count = _required_result_count(
            fix_evidence,
            "backend/.venv/bin/pytest -q backend/tests",
        )

        assert f"Result: `{full_suite_count}" in report


class TestFastAPIContract:
    def test_main_app_registers_post_chat_route(self):
        from app.main import app

        paths = app.openapi()["paths"]

        assert "post" in paths["/chat"]

    def test_main_app_registers_websocket_chat_route(self):
        from app.main import app

        route_paths = set()
        pending_routes = list(app.routes)
        while pending_routes:
            route = pending_routes.pop()
            path = getattr(route, "path", None)
            if path is not None:
                route_paths.add(path)
            pending_routes.extend(getattr(route, "routes", []))
            original_router = getattr(route, "original_router", None)
            if original_router is not None:
                pending_routes.extend(original_router.routes)

        assert "/ws/{character_name}" in route_paths

    def test_root_health_check_returns_ok_for_backend_probe(self, client):
        response = client.get("/")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestLLMClientContract:
    def test_base_client_cannot_be_instantiated_without_generate(self):
        from app.llm.base import LLMClient

        with pytest.raises(TypeError):
            LLMClient()

    def test_generate_signature_matches_router_contract(self):
        from app.llm.base import LLMClient

        signature = inspect.signature(LLMClient.generate)

        assert list(signature.parameters) == [
            "self",
            "system_prompt",
            "user_message",
        ]
        assert signature.return_annotation is str

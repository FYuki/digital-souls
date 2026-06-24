import inspect
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


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


def _required_runtime_evidence_commands() -> set[str]:
    return {
        "backend/.venv/bin/python -m pytest backend/tests/test_memory_rag_service.py backend/tests/test_runtime_contract.py -q",
        "backend/.venv/bin/python -m pytest backend/tests/test_chat.py backend/tests/test_chat_service.py backend/tests/test_ws.py backend/tests/test_ws_integration.py backend/tests/test_chat_integration.py backend/tests/test_memory_rag_runtime_evidence.py backend/tests/test_runtime_contract.py -q",
        "backend/.venv/bin/pytest -q backend/tests",
    }


class TestRuntimeConfiguration:
    def test_repository_has_no_unmerged_index_entries(self):
        result = subprocess.run(
            ["git", "ls-files", "-u"],
            cwd=_BACKEND_DIR.parent,
            check=True,
            capture_output=True,
            text=True,
        )

        assert result.stdout == ""

    def test_repository_status_has_no_unmerged_conflict_codes(self):
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=_BACKEND_DIR.parent,
            check=True,
            capture_output=True,
            text=True,
        )
        conflict_lines = [
            line
            for line in result.stdout.splitlines()
            if line.startswith(("AA ", "UU "))
        ]

        assert conflict_lines == []

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
        import app._chat_runtime as chat_runtime
        import app.memory.rag_service as rag_service
        import app.chat_service as chat_service
        import app.main as main

        rag_source = inspect.getsource(rag_service)
        chat_runtime_source = inspect.getsource(chat_runtime)
        chat_service_source = inspect.getsource(chat_service)
        main_source = inspect.getsource(main)

        assert "resolved_memory_policy" not in rag_source
        assert "resolved_memory_policy" not in chat_service_source
        assert "resolved_memory_policy" in chat_runtime_source
        assert "resolve_chat_runtime_config" in main_source

    def test_chat_service_public_api_exposes_only_chat_entrypoints_without_rag_queue(self):
        import app.chat_service as chat_service

        source = inspect.getsource(chat_service)

        assert "os.environ" not in source
        assert "_DEFAULT_MEMORY_TASK_QUEUE" not in source
        assert "memory_task_queue_scope" not in source
        assert "_ThreadedMemoryTaskQueue" not in source
        assert "_configured_memory_task_queue" not in source
        assert "_queue_lock" not in source
        assert not hasattr(chat_service, "configure_memory_task_queue")
        assert not hasattr(chat_service, "clear_memory_task_queue")
        assert hasattr(chat_service, "generate_chat_reply")
        assert hasattr(chat_service, "create_chat_session")
        assert not hasattr(chat_service, "ChatRuntimeConfig")
        assert not hasattr(chat_service, "ChatService")
        assert not hasattr(chat_service, "ThreadPoolMemoryTaskQueue")
        assert not hasattr(chat_service, "create_chat_service")
        assert "configure_memory_task_queue" not in chat_service.__all__
        assert "clear_memory_task_queue" not in chat_service.__all__
        assert "generate_chat_reply" in chat_service.__all__
        assert "create_chat_session" in chat_service.__all__
        assert "ChatService" not in chat_service.__all__
        assert "ThreadPoolMemoryTaskQueue" not in chat_service.__all__
        assert "create_chat_service" not in chat_service.__all__
        assert "Thread(" not in source
        assert "RuntimeError" not in source

    def test_chat_routes_use_single_chat_service_entrypoints(self):
        import app.chat_service as chat_service
        import app.routers.chat as chat_router
        import app.routers.ws as ws_router

        chat_service_source = inspect.getsource(chat_service)
        chat_router_source = inspect.getsource(chat_router)
        ws_router_source = inspect.getsource(ws_router)

        assert "_generate_chat_reply_for_runtime" not in chat_service_source
        assert "_create_chat_session_for_runtime" not in chat_service_source
        assert "_generate_chat_reply_with_memory_queue" not in chat_service_source
        assert "_create_chat_session_with_memory_queue" not in chat_service_source
        assert "_generate_chat_reply_with_memory_queue" not in chat_router_source
        assert "_create_chat_session_with_memory_queue" not in ws_router_source
        assert "memory_task_queue_scope" not in chat_router_source
        assert "memory_task_queue_scope" not in ws_router_source
        assert "app.chat_runtime" not in chat_router_source
        assert "app.chat_runtime" not in ws_router_source
        assert "request.app.state.chat_service" in chat_router_source
        assert "websocket.app.state.chat_service" in ws_router_source
        assert "generate_chat_reply(" in chat_router_source
        assert "create_chat_session(" in ws_router_source

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

    def test_memory_policy_does_not_expose_raw_service_sections(self):
        import app.memory.memory_policy as memory_policy

        policy = memory_policy.resolved_memory_policy()

        assert not hasattr(policy, "services")

    def test_chat_and_ws_routes_delegate_to_same_app_chat_service(self):
        import app.main as main

        class StubSession:
            def __init__(self, service):
                self._service = service

            def generate_reply(self, message: str) -> str:
                self._service.calls.append(("ws-reply", "miori", message))
                return f"ws:{message}"

        class StubChatService:
            def __init__(self):
                self.calls = []

            def generate_chat_reply(self, character: str, message: str) -> str:
                self.calls.append(("http", character, message))
                return f"http:{message}"

            async def create_chat_session(self, character: str):
                self.calls.append(("ws-open", character))
                return StubSession(self)

        stub_service = StubChatService()
        with TestClient(main.app) as client:
            main.app.state.chat_service = stub_service
            http_response = client.post(
                "/chat",
                json={"character": "miori", "message": "hello"},
            )
            with client.websocket_connect("/ws/miori") as websocket:
                websocket.send_json({"type": "text", "message": "hello"})
                ws_response = websocket.receive_json()

        assert http_response.status_code == 200
        assert http_response.json() == {"character": "miori", "response": "http:hello"}
        assert ws_response == {"type": "text", "response": "ws:hello"}
        assert stub_service.calls == [
            ("http", "miori", "hello"),
            ("ws-open", "miori"),
            ("ws-reply", "miori", "hello"),
        ]

    def test_main_lifespan_shuts_down_chat_service_task_queue_executor(self, monkeypatch):
        import app.main as main

        class RecordingExecutor:
            def __init__(self, *args, **kwargs):
                self.shutdown_called = False

            def shutdown(self, wait: bool) -> None:
                self.shutdown_called = wait

        executor = RecordingExecutor()

        def executor_factory(*args, **kwargs):
            return executor

        monkeypatch.setattr(main, "ThreadPoolExecutor", executor_factory)

        with TestClient(main.app):
            assert executor.shutdown_called is False

        assert executor.shutdown_called is True

    def test_main_lifespan_cleans_runtime_when_config_resolution_fails(self, monkeypatch):
        import app.chat_service as chat_service
        import app.main as main

        class RecordingExecutor:
            def __init__(self, *args, **kwargs):
                self.shutdown_called = False

            def shutdown(self, wait: bool) -> None:
                self.shutdown_called = wait

        executor = RecordingExecutor()

        def executor_factory(*args, **kwargs):
            return executor

        def fail_config_resolution():
            raise ValueError("invalid memory policy")

        monkeypatch.setattr(main, "ThreadPoolExecutor", executor_factory)
        monkeypatch.setattr(
            main._chat_runtime,
            "resolve_chat_runtime_config",
            fail_config_resolution,
        )

        with pytest.raises(ValueError, match="invalid memory policy"):
            with TestClient(main.app):
                raise AssertionError("startup should fail before yielding")

        assert executor.shutdown_called is True
        assert not hasattr(main.app.state, "chat_service")
        with pytest.raises(chat_service.ChatServiceError):
            chat_service.generate_chat_reply("miori", "hello")

    def test_main_lifespan_owns_chat_service_state_and_cleans_it_up(self):
        import app.main as main

        assert not hasattr(main.app.state, "memory_task_queue")
        assert not hasattr(main.app.state, "chat_runtime")
        assert not hasattr(main.app.state, "chat_service")
        with TestClient(main.app):
            assert not hasattr(main.app.state, "memory_task_queue")
            assert not hasattr(main.app.state, "chat_runtime")
            assert hasattr(main.app.state, "chat_service")
        assert not hasattr(main.app.state, "memory_task_queue")
        assert not hasattr(main.app.state, "chat_runtime")
        assert not hasattr(main.app.state, "chat_service")

    def test_main_lifespan_registers_module_entrypoints_to_app_chat_service(self):
        import app.chat_service as chat_service
        import app.main as main

        class StubChatService:
            def generate_chat_reply(self, character: str, message: str) -> str:
                return f"{character}:{message}"

            async def create_chat_session(self, character: str):
                raise AssertionError("not used")

        with TestClient(main.app):
            main.app.state.chat_service = StubChatService()
            assert chat_service.generate_chat_reply("miori", "hello") == "miori:hello"

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
        assert "test_chat_and_ws_routes_delegate_to_same_app_chat_service" in report
        assert "test_main_lifespan_shuts_down_chat_service_task_queue_executor" in report
        assert "test_main_lifespan_cleans_runtime_when_config_resolution_fails" in report
        assert "test_chat_session_uses_same_per_message_resolution_as_http_reply" in report
        assert "test_thread_pool_memory_task_queue_shutdown_waits_for_pending_tasks" in report
        assert "test_runtime_config_fails_fast_for_inconsistent_rag_policy" in report
        assert "test_infra_functions_are_not_public_api" in report
        assert "test_public_generate_chat_reply_delegates_to_configured_service" in report
        assert "test_public_create_chat_session_delegates_to_configured_service" in report
        assert "test_public_entrypoints_fail_fast_without_registered_service" in report
        assert "test_public_entrypoints_follow_registered_app_state_service" in report
        assert "test_public_entrypoints_restore_previous_resolver_after_nested_clear" in report
        assert "test_main_lifespan_registers_module_entrypoints_to_app_chat_service" in report
        assert "test_returns_404_when_character_disappears_after_session_open" in report
        assert "test_two_argument_reply_fails_fast_without_runtime_queue" not in report
        assert "runtime_adapter_uses_app_bound_memory_task_queue" not in report
        assert "Success: no issues found in 41 source files" in report

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

    def test_fix_evidence_keeps_required_runtime_count_commands_as_source(self):
        root = _BACKEND_DIR.parent
        fix_evidence = _read_required_text(root / "fix-evidence.md")

        fix_counts = _passed_result_counts_by_command(fix_evidence)

        assert _required_runtime_evidence_commands().issubset(fix_counts)

    def test_test_report_copies_required_runtime_counts_from_fix_evidence(self):
        root = _BACKEND_DIR.parent
        fix_evidence = _read_required_text(root / "fix-evidence.md")
        report = _read_required_text(root / "test-report.md")

        fix_counts = _passed_result_counts_by_command(fix_evidence)
        report_counts = _passed_result_counts_by_command(report)

        for command in _required_runtime_evidence_commands():
            assert report_counts[command] == fix_counts[command]

    def test_repository_policy_documents_evidence_count_source_of_truth(self):
        root = _BACKEND_DIR.parent
        policy = _read_required_text(root / "docs" / "repository-policy.md")

        assert "`fix-evidence.md` のコマンド出力を単一のソース・オブ・トゥルース" in policy
        assert "`test-report.md` へ同じ値を同時に転記" in policy
        assert "契約テストは固定件数を持たず" in policy

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

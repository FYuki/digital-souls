import ast
from pathlib import Path

import httpx
import pytest


BACKEND_TESTS = Path(__file__).resolve().parents[1]
FRONTEND_SOURCE = Path(__file__).resolve().parents[3] / "frontend" / "src"


def test_backend_tests_are_classified_by_directory() -> None:
    test_files = set(BACKEND_TESTS.rglob("test_*.py"))
    classified_files = set((BACKEND_TESTS / "unit").glob("test_*.py")) | set(
        (BACKEND_TESTS / "module").glob("test_*.py")
    )
    integration_files = set(
        (BACKEND_TESTS / "integration").glob("test_*_integration.py")
    )

    assert test_files == classified_files | integration_files


def test_backend_module_tests_do_not_connect_to_external_services() -> None:
    network_calls = {
        ("httpx", "get"),
        ("httpx", "post"),
        ("requests", "get"),
        ("requests", "post"),
        ("socket", "create_connection"),
    }
    violations: list[str] = []

    for path in (BACKEND_TESTS / "module").glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and (node.func.value.id, node.func.attr) in network_calls
            ):
                violations.append(f"{path.name}:{node.lineno}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "chromadb"
            ):
                violations.append(f"{path.name}:{node.lineno}")

    assert violations == []


def test_backend_integration_dependency_connection_failure_is_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration import test_memory_rag_runtime_evidence_integration

    monkeypatch.setattr(
        test_memory_rag_runtime_evidence_integration.importlib,
        "import_module",
        lambda name: object(),
    )
    request = httpx.Request("GET", "http://127.0.0.1:1/api/tags")

    def fail_connection(*args, **kwargs):
        raise httpx.ConnectError("Ollama is not available", request=request)

    monkeypatch.setattr(
        test_memory_rag_runtime_evidence_integration.httpx,
        "get",
        fail_connection,
    )

    with pytest.raises(httpx.ConnectError, match="Ollama is not available"):
        test_memory_rag_runtime_evidence_integration._require_runtime_evidence_dependencies()


def test_frontend_vitest_files_declare_unit_or_module_layer() -> None:
    test_files = list(FRONTEND_SOURCE.rglob("*.test.ts"))

    assert test_files
    assert all(
        path.name.endswith((".unit.test.ts", ".module.test.ts"))
        for path in test_files
    )

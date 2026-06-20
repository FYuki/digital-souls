import inspect
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).parent.parent


class TestRuntimeConfiguration:
    def test_runtime_requirements_include_fastapi_backend_dependencies(self):
        required = {"fastapi", "uvicorn[standard]", "httpx", "python-dotenv"}

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


class TestFastAPIContract:
    def test_main_app_registers_post_chat_route(self):
        from app.main import app

        paths = app.openapi()["paths"]

        assert "post" in paths["/chat"]

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

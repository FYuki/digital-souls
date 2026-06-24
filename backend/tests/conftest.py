import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "false")
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

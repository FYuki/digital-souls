import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ENVIRONMENTS_DIR = Path(__file__).resolve().parents[2] / "environments"
if str(ENVIRONMENTS_DIR) not in sys.path:
    sys.path.insert(0, str(ENVIRONMENTS_DIR))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "false")
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

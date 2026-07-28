import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker


ENVIRONMENTS_DIR = Path(__file__).resolve().parents[2] / "environments"
if str(ENVIRONMENTS_DIR) not in sys.path:
    sys.path.insert(0, str(ENVIRONMENTS_DIR))


@pytest.fixture(autouse=True)
def conversation_history_database_path(tmp_path, monkeypatch) -> Path:
    database_path = tmp_path / "conversation-history.db"
    monkeypatch.setattr(
        "app.conversation_history.config.DEFAULT_DATABASE_PATH",
        database_path,
    )
    return database_path


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "false")
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def environment_report_validator() -> Draft202012Validator:
    schema_path = ENVIRONMENTS_DIR / "schemas" / "environment-run-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker


ENVIRONMENTS_DIR = Path(__file__).resolve().parents[2] / "environments"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(ENVIRONMENTS_DIR) not in sys.path:
    sys.path.insert(0, str(ENVIRONMENTS_DIR))
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


def _refuse_protected_runtime_data(repository_root: Path) -> None:
    if os.environ.get("DS_ENVIRONMENT_ID") == "dogfood":
        raise pytest.UsageError("pytest fixture refuses dogfood runtime data")
    configured_data_root = os.environ.get("DS_DATA_DIR")
    if configured_data_root is None:
        return
    from app.runtime_data_root import validate_existing_runtime_data_root
    from app.runtime_paths import resolve_runtime_paths

    input_paths = resolve_runtime_paths(os.environ, repository_root)
    if not input_paths.identity_marker_path.exists():
        return
    try:
        validate_existing_runtime_data_root(input_paths, repository_root)
    except ValueError as error:
        raise pytest.UsageError("pytest fixture refuses protected runtime data") from error


@pytest.fixture(autouse=True)
def conversation_history_database_path(tmp_path, monkeypatch) -> Path:
    repository_root = Path(__file__).resolve().parents[2]
    _refuse_protected_runtime_data(repository_root)
    data_root = tmp_path / "runtime-data"
    monkeypatch.setenv("DS_ENVIRONMENT_ID", "test")
    monkeypatch.setenv("DS_DATA_DIR", str(data_root))
    from app.runtime_data_root import initialize_runtime_data_root
    from app.runtime_paths import resolve_runtime_paths

    initialize_runtime_data_root(
        resolve_runtime_paths(os.environ, repository_root), repository_root
    )
    return data_root / "conversation-history.db"


@pytest.fixture
def runtime_paths():
    from app.runtime_paths import resolve_runtime_paths

    repository_root = Path(__file__).resolve().parents[2]
    return resolve_runtime_paths(os.environ, repository_root)


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

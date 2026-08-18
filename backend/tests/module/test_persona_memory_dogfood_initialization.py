import json
import sqlite3
from pathlib import Path

import pytest


def _dogfood_paths(tmp_path: Path):
    from app.runtime_paths import resolve_runtime_paths

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = resolve_runtime_paths(
        {
            "DS_ENVIRONMENT_ID": "dogfood",
            "DS_DATA_DIR": str(tmp_path / "dogfood-data"),
        },
        repository_root,
    )
    return paths, repository_root


def test_persona_schema_initialization_preserves_existing_conversation_database(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.schema import initialize_persona_memory_schema

    paths, repository_root = _dogfood_paths(tmp_path)
    paths.data_root.mkdir()
    paths.identity_marker_path.write_text(
        json.dumps({"schemaVersion": 1, "environmentId": "dogfood"}),
        encoding="utf-8",
    )
    with sqlite3.connect(paths.sqlite_path) as connection:
        connection.execute(
            "CREATE TABLE existing_conversation_history "
            "(id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO existing_conversation_history (id, body) VALUES (?, ?)",
            ("conversation-1", "synthetic-history"),
        )
    conversation_bytes_before = paths.sqlite_path.read_bytes()

    initialize_persona_memory_schema(paths, repository_root)

    conversation_bytes_after = paths.sqlite_path.read_bytes()
    with sqlite3.connect(paths.sqlite_path) as connection:
        existing_row = connection.execute(
            "SELECT id, body FROM existing_conversation_history"
        ).fetchone()
        conversation_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    with sqlite3.connect(paths.persona_memory_sqlite_path) as connection:
        persona_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    assert conversation_bytes_after == conversation_bytes_before
    assert existing_row == ("conversation-1", "synthetic-history")
    assert conversation_tables == {"existing_conversation_history"}
    assert persona_tables == {
        "approved_memories",
        "memory_sources",
        "memory_lineage",
        "memory_index_outbox",
        "temporary_provider_records",
    }


def test_identity_mismatch_fails_before_creating_or_repairing_runtime_artifacts(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.schema import initialize_persona_memory_schema

    paths, repository_root = _dogfood_paths(tmp_path)
    paths.data_root.mkdir()
    marker = {"schemaVersion": 1, "environmentId": "dev"}
    paths.identity_marker_path.write_text(json.dumps(marker), encoding="utf-8")
    entries_before = {entry.name for entry in paths.data_root.iterdir()}

    with pytest.raises(ValueError, match="environment identity"):
        initialize_persona_memory_schema(paths, repository_root)

    entries_after = {entry.name for entry in paths.data_root.iterdir()}
    assert json.loads(paths.identity_marker_path.read_text(encoding="utf-8")) == marker
    assert entries_after == entries_before
    assert not paths.sqlite_path.exists()
    assert not paths.persona_memory_sqlite_path.exists()

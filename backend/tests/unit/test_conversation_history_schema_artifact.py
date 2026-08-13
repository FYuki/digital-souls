from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.conversation_history import schema
from tests.backup_restore_test_support import create_version_two_database


@pytest.mark.parametrize("artifact_state", ("valid", "invalid"))
def test_schema_api_01_inspects_valid_and_invalid_artifacts_through_public_api(
    tmp_path: Path,
    artifact_state: str,
) -> None:
    artifact = tmp_path / "backup-artifact.sqlite3"
    create_version_two_database(artifact)
    if artifact_state == "invalid":
        with sqlite3.connect(artifact) as connection:
            connection.execute("DROP INDEX conversation_turns_history_idx")

    inspect_artifact = schema.inspect_conversation_history_artifact_schema
    inspection = inspect_artifact(artifact)

    assert inspection.is_current is False
    assert inspection.migration_required is (artifact_state == "valid")

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from app.backup_restore.models import BackupSchemaError, BackupVerification
from app.conversation_history.schema import (
    inspect_conversation_history_artifact_schema,
)


def create_sqlite_snapshot(source: Path, destination: Path) -> None:
    try:
        with closing(
            sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        ) as source_connection:
            with closing(sqlite3.connect(destination)) as destination_connection:
                source_connection.backup(destination_connection)
    except sqlite3.Error as error:
        raise BackupSchemaError("SQLite snapshot could not be created") from error


def verify_sqlite_database(database_path: Path) -> BackupVerification:
    try:
        with closing(
            sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        ) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise BackupSchemaError("SQLite integrity check failed")
            conversation_count = int(
                connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            )
    except BackupSchemaError:
        raise
    except sqlite3.Error as error:
        raise BackupSchemaError("SQLite schema validation failed") from error
    inspection = inspect_conversation_history_artifact_schema(database_path)
    if not inspection.is_current and not inspection.migration_required:
        raise BackupSchemaError("SQLite schema version or table contract does not match")
    return BackupVerification(
        integrity_check=integrity,
        schema_version=inspection.schema_version,
        required_tables=inspection.tables,
        conversation_count=conversation_count,
    )

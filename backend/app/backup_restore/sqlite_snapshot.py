from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from app.backup_restore.models import (
    CONVERSATION_ARTIFACT_FILENAME,
    PERSONA_MEMORY_ARTIFACT_FILENAME,
    BackupSchemaError,
    BackupVerification,
)
from app.conversation_history.schema import (
    inspect_conversation_history_artifact_schema,
)
from app.memory.persistence.schema import (
    PERSONA_MEMORY_TABLES,
    SCHEMA_VERSION as PERSONA_MEMORY_SCHEMA_VERSION,
)


def _read_only_uri(database_path: Path) -> str:
    return f"{database_path.resolve().as_uri()}?mode=ro"


def create_sqlite_snapshot(source: Path, destination: Path) -> None:
    try:
        with closing(
            sqlite3.connect(_read_only_uri(source), uri=True)
        ) as source_connection:
            with closing(sqlite3.connect(destination)) as destination_connection:
                source_connection.backup(destination_connection)
                journal_mode_row = destination_connection.execute(
                    "PRAGMA journal_mode = DELETE"
                ).fetchone()
                journal_mode = (
                    None
                    if journal_mode_row is None
                    else str(journal_mode_row[0]).lower()
                )
                if journal_mode != "delete":
                    raise BackupSchemaError(
                        "SQLite snapshot journal mode could not be set"
                    )
    except sqlite3.Error as error:
        raise BackupSchemaError("SQLite snapshot could not be created") from error


def verify_sqlite_database(
    database_path: Path,
    artifact_filename: str,
) -> BackupVerification:
    try:
        with closing(
            sqlite3.connect(_read_only_uri(database_path), uri=True)
        ) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise BackupSchemaError("SQLite integrity check failed")
            if artifact_filename == CONVERSATION_ARTIFACT_FILENAME:
                record_count = int(
                    connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
                )
            elif artifact_filename == PERSONA_MEMORY_ARTIFACT_FILENAME:
                record_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM approved_memories"
                    ).fetchone()[0]
                )
            else:
                raise BackupSchemaError("SQLite artifact type is unsupported")
    except BackupSchemaError:
        raise
    except sqlite3.Error as error:
        raise BackupSchemaError("SQLite schema validation failed") from error
    if artifact_filename == CONVERSATION_ARTIFACT_FILENAME:
        inspection = inspect_conversation_history_artifact_schema(database_path)
        if not inspection.is_current and not inspection.migration_required:
            raise BackupSchemaError(
                "SQLite schema version or table contract does not match"
            )
        schema_version = inspection.schema_version
        tables = inspection.tables
    elif artifact_filename == PERSONA_MEMORY_ARTIFACT_FILENAME:
        with closing(
            sqlite3.connect(_read_only_uri(database_path), uri=True)
        ) as connection:
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = frozenset(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            )
        if (
            schema_version != PERSONA_MEMORY_SCHEMA_VERSION
            or tables != PERSONA_MEMORY_TABLES
        ):
            raise BackupSchemaError(
                "SQLite schema version or table contract does not match"
            )
    else:
        raise BackupSchemaError("SQLite artifact type is unsupported")
    return BackupVerification(
        filename=artifact_filename,
        integrity_check=integrity,
        schema_version=schema_version,
        required_tables=tables,
        record_count=record_count,
    )

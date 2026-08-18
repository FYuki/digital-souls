from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.memory.persistence.contracts import (
    TemporaryProviderRecord,
    TemporaryProviderRecordInput,
)
from app.memory.persistence.sqlite import (
    ConnectionFactory,
    PersonaMemorySqlite,
    format_datetime,
    parse_datetime,
)


Clock = Callable[[], datetime]
UuidFactory = Callable[[], UUID]
TEMPORARY_PROVIDERS = frozenset(
    {"temporary:agriculture", "temporary:recipe"}
)
TEMPORARY_COLUMNS = (
    "id, character_id, provider_id, source_ref, record_type, structured_value, "
    "effective_at, created_at, updated_at"
)


class TemporaryProviderRecordRepository:
    def __init__(
        self,
        *,
        database_path: Path,
        clock: Clock,
        uuid_factory: UuidFactory,
        connection_factory: ConnectionFactory = sqlite3.connect,
    ) -> None:
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._database = PersonaMemorySqlite(database_path, connection_factory)

    def save(
        self,
        *,
        character_id: str,
        record: TemporaryProviderRecordInput,
    ) -> TemporaryProviderRecord:
        _require_character_id(character_id)
        _require_provider_id(record.provider_id)
        record_id = self._new_uuid()
        now = self._now()
        timestamp = format_datetime(now)
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO temporary_provider_records "
                "(id, character_id, provider_id, source_ref, record_type, "
                "structured_value, effective_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(record_id),
                    character_id,
                    record.provider_id,
                    record.source_ref,
                    record.record_type,
                    record.structured_value,
                    format_datetime(record.effective_at),
                    timestamp,
                    timestamp,
                ),
            )
            return _select_record(
                connection,
                character_id,
                record.provider_id,
                record_id,
            )

    def hard_delete_after_migration(
        self,
        *,
        character_id: str,
        provider_id: str,
        record_id: UUID,
    ) -> None:
        _require_character_id(character_id)
        _require_provider_id(provider_id)
        _require_uuid4(record_id)
        with self._database.transaction() as connection:
            _select_record(connection, character_id, provider_id, record_id)
            connection.execute(
                "DELETE FROM temporary_provider_records "
                "WHERE character_id = ? AND provider_id = ? AND id = ?",
                (character_id, provider_id, str(record_id)),
            )
        self._database.truncate_wal()

    def _new_uuid(self) -> UUID:
        value = self._uuid_factory()
        _require_uuid4(value)
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _select_record(
    connection: sqlite3.Connection,
    character_id: str,
    provider_id: str,
    record_id: UUID,
) -> TemporaryProviderRecord:
    row = connection.execute(
        f"SELECT {TEMPORARY_COLUMNS} FROM temporary_provider_records "
        "WHERE character_id = ? AND provider_id = ? AND id = ?",
        (character_id, provider_id, str(record_id)),
    ).fetchone()
    if row is None:
        raise LookupError("temporary provider record was not found")
    return TemporaryProviderRecord(
        id=UUID(str(row[0])),
        character_id=str(row[1]),
        provider_id=str(row[2]),
        source_ref=str(row[3]),
        record_type=str(row[4]),
        structured_value=str(row[5]),
        effective_at=parse_datetime(str(row[6])),
        created_at=parse_datetime(str(row[7])),
        updated_at=parse_datetime(str(row[8])),
    )


def _require_character_id(character_id: str) -> None:
    if not isinstance(character_id, str) or not character_id.strip():
        raise ValueError("character_id must not be empty")


def _require_provider_id(provider_id: str) -> None:
    if provider_id not in TEMPORARY_PROVIDERS:
        raise ValueError("provider_id must be a temporary provider")


def _require_uuid4(value: UUID) -> None:
    if not isinstance(value, UUID) or value.version != 4:
        raise ValueError("record_id must be a UUID4")

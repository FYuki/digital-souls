from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from app.memory.persistence.contracts import (
    TemporaryProviderRecord,
    TemporaryProviderRecordCorrection,
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

    def list_by_provider(
        self, *, character_id: str, provider_id: str
    ) -> list[TemporaryProviderRecord]:
        _require_character_id(character_id)
        _require_provider_id(provider_id)
        with self._database.connection() as connection:
            rows = connection.execute(
                f"SELECT {TEMPORARY_COLUMNS} FROM temporary_provider_records "
                "WHERE character_id = ? AND provider_id = ? ORDER BY created_at, id",
                (character_id, provider_id),
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def get(
        self, *, character_id: str, provider_id: str, record_id: UUID
    ) -> TemporaryProviderRecord | None:
        _require_character_id(character_id)
        _require_provider_id(provider_id)
        _require_uuid4(record_id)
        with self._database.connection() as connection:
            row = _find_record(connection, character_id, provider_id, record_id)
        return None if row is None else _record_from_row(row)

    def correct(
        self,
        *,
        character_id: str,
        provider_id: str,
        record_id: UUID,
        correction: TemporaryProviderRecordCorrection,
    ) -> TemporaryProviderRecord:
        _require_character_id(character_id)
        _require_provider_id(provider_id)
        _require_uuid4(record_id)
        if not isinstance(correction, TemporaryProviderRecordCorrection):
            raise TypeError("correction must be a TemporaryProviderRecordCorrection")
        with self._database.transaction() as connection:
            current = _select_record(connection, character_id, provider_id, record_id)
            if (
                current.record_type == correction.record_type
                and current.structured_value == correction.structured_value
                and current.effective_at == correction.effective_at
            ):
                return current
            connection.execute(
                "UPDATE temporary_provider_records SET record_type = ?, "
                "structured_value = ?, effective_at = ?, updated_at = ? "
                "WHERE character_id = ? AND provider_id = ? AND id = ?",
                (
                    correction.record_type,
                    correction.structured_value,
                    format_datetime(correction.effective_at),
                    format_datetime(self._now()),
                    character_id,
                    provider_id,
                    str(record_id),
                ),
            )
            return _select_record(connection, character_id, provider_id, record_id)

    def hard_delete(
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
    row = _find_record(connection, character_id, provider_id, record_id)
    if row is None:
        raise LookupError("temporary provider record was not found")
    return _record_from_row(row)


def _find_record(
    connection: sqlite3.Connection,
    character_id: str,
    provider_id: str,
    record_id: UUID,
) -> sqlite3.Row | None:
    row = connection.execute(
        f"SELECT {TEMPORARY_COLUMNS} FROM temporary_provider_records "
        "WHERE character_id = ? AND provider_id = ? AND id = ?",
        (character_id, provider_id, str(record_id)),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def _record_from_row(row: sqlite3.Row) -> TemporaryProviderRecord:
    return TemporaryProviderRecord(
        id=UUID(str(row["id"])),
        character_id=str(row["character_id"]),
        provider_id=str(row["provider_id"]),
        source_ref=str(row["source_ref"]),
        record_type=str(row["record_type"]),
        structured_value=str(row["structured_value"]),
        effective_at=parse_datetime(str(row["effective_at"])),
        created_at=parse_datetime(str(row["created_at"])),
        updated_at=parse_datetime(str(row["updated_at"])),
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

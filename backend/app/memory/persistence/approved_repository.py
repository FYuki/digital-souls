from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from app.memory.admission.contracts import (
    ApprovedMemoryCandidate,
    EpisodicEventType,
    EpisodicEventValue,
    EpisodicSubject,
    InteractionAspect,
    InteractionPreferenceValue,
    MemoryType,
    PreferencePolarity,
    StructuredValue,
    UserPreferenceValue,
)
from app.memory.persistence.contracts import (
    ApprovedMemory,
    ApprovedMemoryDetail,
    MemoryLineageInput,
    MemoryLineageRelation,
    MemorySourceInput,
    MemorySourceType,
    MemoryStatus,
    MemoryWriteContext,
    TemporalPrecision,
)
from app.memory.persistence.sqlite import (
    ConnectionFactory,
    PersonaMemorySqlite,
    format_datetime,
    parse_datetime,
)


Clock = Callable[[], datetime]
UuidFactory = Callable[[], UUID]
APPROVED_COLUMN_NAMES = (
    "id",
    "character_id",
    "provider_id",
    "memory_kind",
    "memory_type",
    "structured_value",
    "normalized_text",
    "policy_version",
    "content_version",
    "status",
    "effective_at",
    "effective_timezone",
    "temporal_precision",
    "expires_at",
    "last_user_mentioned_at",
    "created_at",
    "updated_at",
)
APPROVED_COLUMNS = ", ".join(
    f"{column} AS {column}" for column in APPROVED_COLUMN_NAMES
)
QUALIFIED_APPROVED_COLUMNS = ", ".join(
    f"m.{column} AS {column}" for column in APPROVED_COLUMN_NAMES
)


class ApprovedMemoryRepository:
    def __init__(
        self,
        *,
        database_path: Path,
        clock: Clock,
        uuid_factory: UuidFactory,
        outbox_uuid_factory: UuidFactory,
        connection_factory: ConnectionFactory = sqlite3.connect,
    ) -> None:
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._outbox_uuid_factory = outbox_uuid_factory
        self._database = PersonaMemorySqlite(database_path, connection_factory)

    def save(
        self,
        *,
        character_id: str,
        candidate: ApprovedMemoryCandidate,
        context: MemoryWriteContext,
    ) -> ApprovedMemory:
        _require_character_id(character_id)
        _require_approved_candidate(candidate)
        memory_id = self._new_uuid(self._uuid_factory)
        now = self._now()
        memory_type, memory_kind, episodic_event_type = _candidate_classification(
            candidate
        )
        with self._database.transaction() as connection:
            existing = _select_memory_by_write_key(
                connection,
                character_id,
                context.idempotency_key,
            )
            if existing is not None:
                return existing
            insert_result = connection.execute(
                "INSERT INTO approved_memories ("
                "id, character_id, provider_id, memory_kind, memory_type, "
                "episodic_event_type, formation_method, schema_version, "
                "normalized_text, structured_value, policy_version, "
                "classifier_version, model_id, model_digest, prompt_version, "
                "content_version, status, idempotency_key, effective_at, "
                "effective_timezone, temporal_precision, expires_at, "
                "last_user_mentioned_at, last_consolidated_at, created_at, updated_at"
                ") VALUES (?, ?, 'core', ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, "
                "1, 'ACTIVE', ?, ?, ?, ?, ?, ?, NULL, ?, ?) "
                "ON CONFLICT(character_id, idempotency_key) DO NOTHING",
                (
                    str(memory_id),
                    character_id,
                    memory_kind,
                    memory_type.value,
                    episodic_event_type,
                    context.formation_method.value,
                    candidate.normalized_text,
                    _serialize_structured_value(candidate.structured_value),
                    context.policy_version,
                    context.classifier_version,
                    context.model_id,
                    context.model_digest,
                    context.prompt_version,
                    context.idempotency_key,
                    format_datetime(context.effective_at),
                    context.effective_timezone,
                    context.temporal_precision.value,
                    _format_optional_datetime(context.expires_at),
                    format_datetime(now),
                    format_datetime(now),
                    format_datetime(now),
                ),
            )
            if insert_result.rowcount == 1:
                self._insert_write_receipt(
                    connection,
                    memory_id,
                    character_id,
                    context.idempotency_key,
                    "SAVE",
                    now,
                )
                self._insert_sources(
                    connection,
                    memory_id,
                    character_id,
                    context.sources,
                )
                self._insert_lineage(
                    connection,
                    memory_id,
                    character_id,
                    context.lineage,
                )
                self._insert_outbox(
                    connection,
                    memory_id,
                    character_id,
                    "UPSERT",
                    now,
                )
            return _select_memory(connection, character_id, memory_id)

    def correct(
        self,
        *,
        character_id: str,
        memory_id: UUID,
        candidate: ApprovedMemoryCandidate,
        context: MemoryWriteContext,
    ) -> ApprovedMemory:
        _require_character_id(character_id)
        _require_uuid4(memory_id)
        _require_approved_candidate(candidate)
        now = self._now()
        memory_type, memory_kind, episodic_event_type = _candidate_classification(
            candidate
        )
        with self._database.transaction() as connection:
            current = _select_memory(connection, character_id, memory_id)
            existing = _select_memory_by_write_key(
                connection,
                character_id,
                context.idempotency_key,
            )
            if existing is not None:
                if existing.id != memory_id:
                    raise ValueError("idempotency key belongs to another memory")
                return current
            connection.execute(
                "UPDATE approved_memories SET memory_kind = ?, memory_type = ?, "
                "episodic_event_type = ?, formation_method = ?, normalized_text = ?, "
                "structured_value = ?, policy_version = ?, classifier_version = ?, "
                "model_id = ?, model_digest = ?, prompt_version = ?, "
                "content_version = content_version + 1, "
                "last_write_idempotency_key = ?, "
                "effective_at = ?, effective_timezone = ?, temporal_precision = ?, "
                "expires_at = ?, updated_at = ? "
                "WHERE character_id = ? AND id = ?",
                (
                    memory_kind,
                    memory_type.value,
                    episodic_event_type,
                    context.formation_method.value,
                    candidate.normalized_text,
                    _serialize_structured_value(candidate.structured_value),
                    context.policy_version,
                    context.classifier_version,
                    context.model_id,
                    context.model_digest,
                    context.prompt_version,
                    context.idempotency_key,
                    format_datetime(context.effective_at),
                    context.effective_timezone,
                    context.temporal_precision.value,
                    _format_optional_datetime(context.expires_at),
                    format_datetime(now),
                    character_id,
                    str(memory_id),
                ),
            )
            self._insert_write_receipt(
                connection,
                memory_id,
                character_id,
                context.idempotency_key,
                "CORRECT",
                now,
            )
            self._insert_sources(
                connection,
                memory_id,
                character_id,
                context.sources,
            )
            self._insert_lineage(
                connection,
                memory_id,
                character_id,
                context.lineage,
            )
            self._insert_outbox(connection, memory_id, character_id, "UPSERT", now)
            return _select_memory(connection, character_id, memory_id)

    def touch(
        self,
        *,
        character_id: str,
        memory_id: UUID,
        candidate: ApprovedMemoryCandidate,
        mentioned_at: datetime,
    ) -> ApprovedMemory:
        _require_character_id(character_id)
        _require_uuid4(memory_id)
        _require_approved_candidate(candidate)
        mentioned_at_text = format_datetime(mentioned_at)
        with self._database.transaction() as connection:
            current = _select_memory(connection, character_id, memory_id)
            if (
                current.normalized_text != candidate.normalized_text
                or current.structured_value != candidate.structured_value
            ):
                raise ValueError("TOUCH candidate does not match approved memory")
            connection.execute(
                "UPDATE approved_memories SET last_user_mentioned_at = ? "
                "WHERE character_id = ? AND id = ?",
                (mentioned_at_text, character_id, str(memory_id)),
            )
            return _select_memory(connection, character_id, memory_id)

    def deactivate(self, *, character_id: str, memory_id: UUID) -> ApprovedMemory:
        _require_character_id(character_id)
        _require_uuid4(memory_id)
        with self._database.transaction() as connection:
            _select_memory(connection, character_id, memory_id)
            connection.execute(
                "UPDATE approved_memories SET status = 'INACTIVE' "
                "WHERE character_id = ? AND id = ?",
                (character_id, str(memory_id)),
            )
            return _select_memory(connection, character_id, memory_id)

    def get(
        self,
        *,
        character_id: str,
        memory_id: UUID,
    ) -> ApprovedMemory | None:
        _require_character_id(character_id)
        _require_uuid4(memory_id)
        with self._database.connection() as connection:
            row = connection.execute(
                f"SELECT {APPROVED_COLUMNS} FROM approved_memories "
                "WHERE character_id = ? AND id = ?",
                (character_id, str(memory_id)),
            ).fetchone()
        return None if row is None else _memory_from_row(row)

    def list_active(self, *, character_id: str) -> list[ApprovedMemory]:
        _require_character_id(character_id)
        now = format_datetime(self._now())
        with self._database.connection() as connection:
            rows = connection.execute(
                f"SELECT {APPROVED_COLUMNS} FROM approved_memories "
                "WHERE character_id = ? AND status = 'ACTIVE' "
                "AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY created_at, id",
                (character_id, now),
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def list_by_provider(
        self,
        *,
        character_id: str,
        provider_id: str,
        status: MemoryStatus,
    ) -> list[ApprovedMemory]:
        _require_character_id(character_id)
        _require_core_provider(provider_id)
        if not isinstance(status, MemoryStatus):
            raise TypeError("status must be a MemoryStatus")
        with self._database.connection() as connection:
            rows = connection.execute(
                f"SELECT {APPROVED_COLUMNS} FROM approved_memories "
                "WHERE character_id = ? AND provider_id = ? AND status = ? "
                "ORDER BY created_at, id",
                (character_id, provider_id, status.value),
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def get_detail(
        self,
        *,
        character_id: str,
        provider_id: str,
        memory_id: UUID,
    ) -> ApprovedMemoryDetail | None:
        _require_character_id(character_id)
        _require_core_provider(provider_id)
        _require_uuid4(memory_id)
        with self._database.connection() as connection:
            row = connection.execute(
                f"SELECT {APPROVED_COLUMNS} FROM approved_memories "
                "WHERE character_id = ? AND provider_id = ? AND id = ?",
                (character_id, provider_id, str(memory_id)),
            ).fetchone()
            if row is None:
                return None
            source_rows = connection.execute(
                "SELECT source_type, source_provider_id, source_ref "
                "FROM memory_sources WHERE character_id = ? AND memory_id = ? "
                "ORDER BY source_type, source_provider_id, source_ref",
                (character_id, str(memory_id)),
            ).fetchall()
            lineage_rows = connection.execute(
                "SELECT related_memory_id, relation FROM memory_lineage "
                "WHERE character_id = ? AND memory_id = ? "
                "ORDER BY relation, related_memory_id",
                (character_id, str(memory_id)),
            ).fetchall()
        return ApprovedMemoryDetail(
            memory=_memory_from_row(row),
            sources=tuple(
                MemorySourceInput(
                    source_type=MemorySourceType(str(source["source_type"])),
                    source_provider_id=str(source["source_provider_id"]),
                    source_ref=str(source["source_ref"]),
                )
                for source in source_rows
            ),
            lineage=tuple(
                MemoryLineageInput(
                    related_memory_id=UUID(str(item["related_memory_id"])),
                    relation=MemoryLineageRelation(str(item["relation"])),
                )
                for item in lineage_rows
            ),
        )

    def is_index_pending(self, *, character_id: str, memory_id: UUID) -> bool:
        _require_character_id(character_id)
        _require_uuid4(memory_id)
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM memory_index_outbox WHERE character_id = ? "
                "AND memory_id = ? AND status IN ('PENDING', 'FAILED') LIMIT 1",
                (character_id, str(memory_id)),
            ).fetchone()
        return row is not None

    def list_character_ids(self) -> set[str]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT character_id FROM approved_memories"
            ).fetchall()
        return {str(row["character_id"]) for row in rows}

    def hard_delete(self, *, character_id: str, memory_id: UUID) -> None:
        _require_character_id(character_id)
        _require_uuid4(memory_id)
        now = self._now()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM approved_memories WHERE character_id = ? AND id = ?",
                (character_id, str(memory_id)),
            ).fetchone()
            if row is None:
                return
            self._insert_outbox(connection, memory_id, character_id, "DELETE", now)
            connection.execute(
                "DELETE FROM approved_memories WHERE character_id = ? AND id = ?",
                (character_id, str(memory_id)),
            )
        self._database.truncate_wal()

    def _insert_outbox(
        self,
        connection: sqlite3.Connection,
        memory_id: UUID,
        character_id: str,
        operation: str,
        now: datetime,
    ) -> None:
        timestamp = format_datetime(now)
        connection.execute(
            "INSERT INTO memory_index_outbox "
            "(id, memory_id, character_id, operation, status, attempt_count, "
            "last_error_code, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'PENDING', 0, NULL, ?, ?)",
            (
                str(self._new_uuid(self._outbox_uuid_factory)),
                str(memory_id),
                character_id,
                operation,
                timestamp,
                timestamp,
            ),
        )

    def _insert_write_receipt(
        self,
        connection: sqlite3.Connection,
        memory_id: UUID,
        character_id: str,
        idempotency_key: str,
        operation: str,
        now: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO memory_write_receipts "
            "(character_id, idempotency_key, memory_id, operation, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                character_id,
                idempotency_key,
                str(memory_id),
                operation,
                format_datetime(now),
            ),
        )

    def _insert_sources(
        self,
        connection: sqlite3.Connection,
        memory_id: UUID,
        character_id: str,
        sources: tuple[MemorySourceInput, ...],
    ) -> None:
        connection.executemany(
            "INSERT INTO memory_sources "
            "(character_id, memory_id, source_type, source_provider_id, source_ref) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(character_id, memory_id, source_type, "
            "source_provider_id, source_ref) DO NOTHING",
            (
                (
                    character_id,
                    str(memory_id),
                    source.source_type.value,
                    source.source_provider_id,
                    source.source_ref,
                )
                for source in sources
            ),
        )

    def _insert_lineage(
        self,
        connection: sqlite3.Connection,
        memory_id: UUID,
        character_id: str,
        lineage: tuple[MemoryLineageInput, ...],
    ) -> None:
        connection.executemany(
            "INSERT INTO memory_lineage "
            "(character_id, memory_id, related_memory_id, relation) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(character_id, memory_id, related_memory_id, relation) "
            "DO NOTHING",
            (
                (
                    character_id,
                    str(memory_id),
                    str(item.related_memory_id),
                    item.relation.value,
                )
                for item in lineage
            ),
        )

    def _new_uuid(self, factory: UuidFactory) -> UUID:
        value = factory()
        _require_uuid4(value)
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _select_memory(
    connection: sqlite3.Connection,
    character_id: str,
    memory_id: UUID,
) -> ApprovedMemory:
    row = connection.execute(
        f"SELECT {APPROVED_COLUMNS} FROM approved_memories "
        "WHERE character_id = ? AND id = ?",
        (character_id, str(memory_id)),
    ).fetchone()
    if row is None:
        raise LookupError("approved memory was not found")
    return _memory_from_row(row)


def _select_memory_by_write_key(
    connection: sqlite3.Connection,
    character_id: str,
    idempotency_key: str,
) -> ApprovedMemory | None:
    row = connection.execute(
        f"SELECT {QUALIFIED_APPROVED_COLUMNS} "
        "FROM memory_write_receipts AS receipt "
        "JOIN approved_memories AS m "
        "ON m.character_id = receipt.character_id AND m.id = receipt.memory_id "
        "WHERE receipt.character_id = ? AND receipt.idempotency_key = ?",
        (character_id, idempotency_key),
    ).fetchone()
    return None if row is None else _memory_from_row(row)


def _memory_from_row(row: sqlite3.Row) -> ApprovedMemory:
    memory_type = MemoryType(str(row["memory_type"]))
    return ApprovedMemory(
        id=UUID(str(row["id"])),
        character_id=str(row["character_id"]),
        provider_id=str(row["provider_id"]),
        memory_kind=str(row["memory_kind"]),
        memory_type=memory_type,
        structured_value=_deserialize_structured_value(
            memory_type,
            str(row["structured_value"]),
        ),
        normalized_text=str(row["normalized_text"]),
        policy_version=str(row["policy_version"]),
        content_version=int(row["content_version"]),
        status=MemoryStatus(str(row["status"])),
        effective_at=parse_datetime(str(row["effective_at"])),
        effective_timezone=str(row["effective_timezone"]),
        temporal_precision=TemporalPrecision(str(row["temporal_precision"])),
        expires_at=(
            None
            if row["expires_at"] is None
            else parse_datetime(str(row["expires_at"]))
        ),
        last_user_mentioned_at=(
            None
            if row["last_user_mentioned_at"] is None
            else parse_datetime(str(row["last_user_mentioned_at"]))
        ),
        created_at=parse_datetime(str(row["created_at"])),
        updated_at=parse_datetime(str(row["updated_at"])),
    )


def _candidate_classification(
    candidate: ApprovedMemoryCandidate,
) -> tuple[MemoryType, str, str | None]:
    structured_value = candidate.structured_value
    if isinstance(structured_value, EpisodicEventValue):
        return MemoryType.EPISODIC_EVENT, "EPISODIC", structured_value.event_type.value
    if isinstance(structured_value, UserPreferenceValue):
        return MemoryType.USER_PREFERENCE, "SEMANTIC", None
    if isinstance(structured_value, InteractionPreferenceValue):
        return MemoryType.INTERACTION_PREFERENCE, "SEMANTIC", None
    raise TypeError("candidate must contain an allowlist structured value")


def _serialize_structured_value(value: StructuredValue) -> str:
    return json.dumps(
        asdict(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _deserialize_structured_value(
    memory_type: MemoryType,
    serialized: str,
) -> StructuredValue:
    raw = cast(dict[str, object], json.loads(serialized))
    if memory_type is MemoryType.EPISODIC_EVENT:
        return EpisodicEventValue(
            event_type=EpisodicEventType(str(raw["event_type"])),
            subject=EpisodicSubject(str(raw["subject"])),
            topic=str(raw["topic"]),
        )
    if memory_type is MemoryType.USER_PREFERENCE:
        alternative = raw.get("alternative")
        return UserPreferenceValue(
            polarity=PreferencePolarity(str(raw["polarity"])),
            object=str(raw["object"]),
            alternative=None if alternative is None else str(alternative),
        )
    return InteractionPreferenceValue(
        aspect=InteractionAspect(str(raw["aspect"])),
        value=str(raw["value"]),
    )


def _format_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else format_datetime(value)


def _require_approved_candidate(candidate: object) -> None:
    if not isinstance(candidate, ApprovedMemoryCandidate):
        raise TypeError("candidate must be an ApprovedMemoryCandidate")


def _require_character_id(character_id: str) -> None:
    if not isinstance(character_id, str) or not character_id.strip():
        raise ValueError("character_id must not be empty")


def _require_core_provider(provider_id: str) -> None:
    if provider_id != "core":
        raise ValueError("provider_id must be core")


def _require_uuid4(value: UUID) -> None:
    if not isinstance(value, UUID) or value.version != 4:
        raise ValueError("identifier must be a UUID4")

import hashlib
import logging
import sqlite3
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from uuid import UUID

import pytest

from app.memory.admission.contracts import (
    ApprovedMemoryCandidate,
    PreferencePolarity,
    UserPreferenceValue,
)


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
PRIVATE_MARKER = "PRIVATE-CONTENT-MARKER"
PRIVATE_SOURCE_HASH = hashlib.sha256(PRIVATE_MARKER.encode("utf-8")).hexdigest()


def _setup(tmp_path: Path, *, connection_factory=sqlite3.connect):
    from app.memory.persistence.approved_repository import ApprovedMemoryRepository
    from app.memory.persistence.schema import initialize_persona_memory_schema
    from app.runtime_paths import resolve_runtime_paths

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = resolve_runtime_paths(
        {
            "DS_ENVIRONMENT_ID": "test",
            "DS_DATA_DIR": str(tmp_path / "data"),
        },
        repository_root,
    )
    initialize_persona_memory_schema(paths, repository_root)
    identifiers = count(1)
    repository = ApprovedMemoryRepository(
        database_path=paths.persona_memory_sqlite_path,
        clock=lambda: NOW,
        uuid_factory=lambda: UUID(
            f"00000000-0000-4000-8000-{next(identifiers):012d}"
        ),
        outbox_uuid_factory=lambda: UUID(
            f"10000000-0000-4000-8000-{next(identifiers):012d}"
        ),
        connection_factory=connection_factory,
    )
    return repository, paths.persona_memory_sqlite_path


def _candidate() -> ApprovedMemoryCandidate:
    return ApprovedMemoryCandidate(
        structured_value=UserPreferenceValue(
            polarity=PreferencePolarity.LIKE,
            object=PRIVATE_MARKER,
        ),
        normalized_text=PRIVATE_MARKER,
    )


def _context(*, effective_at: datetime = NOW, expires_at: datetime | None = None):
    from app.memory.persistence.contracts import (
        FormationMethod,
        MemorySourceInput,
        MemorySourceType,
        MemoryWriteContext,
        TemporalPrecision,
    )

    return MemoryWriteContext(
        formation_method=FormationMethod.EXTRACTED,
        idempotency_key="conversation-1:turn-1:0:extractor-v1",
        effective_at=effective_at,
        effective_timezone="Asia/Tokyo",
        temporal_precision=TemporalPrecision.SECOND,
        expires_at=expires_at,
        policy_version="policy-v1",
        classifier_version="classifier-v1",
        model_id="gemma4:e4b",
        model_digest="model-digest",
        prompt_version="prompt-v1",
        sources=(
            MemorySourceInput(
                source_type=MemorySourceType.CONVERSATION_TURN,
                source_provider_id="core",
                source_ref="conversation-1:turn-1",
            ),
        ),
    )


class _InspectableConnection(sqlite3.Connection):
    def close(self) -> None:
        return None


def test_repository_connections_enable_foreign_keys_secure_delete_and_wal(
    tmp_path: Path,
) -> None:
    opened: list[_InspectableConnection] = []

    def connection_factory(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, factory=_InspectableConnection)
        opened.append(connection)
        return connection

    repository, _database_path = _setup(
        tmp_path, connection_factory=connection_factory
    )

    repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )

    assert opened[-1].execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert opened[-1].execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert opened[-1].execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    for connection in opened:
        sqlite3.Connection.close(connection)


def test_hard_delete_finishes_with_a_truncated_wal(tmp_path: Path) -> None:
    repository, database_path = _setup(tmp_path)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )

    repository.hard_delete(character_id="miori", memory_id=memory.id)

    wal_path = Path(f"{database_path}-wal")
    with sqlite3.connect(database_path) as connection:
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    assert journal_mode.lower() == "wal"
    assert not wal_path.exists() or wal_path.stat().st_size == 0


def test_write_context_rejects_timezone_naive_datetimes(
) -> None:
    with pytest.raises(ValueError):
        _context(effective_at=datetime(2026, 8, 18, 12, 0))


def test_write_context_rejects_timezone_naive_expiration() -> None:
    with pytest.raises(ValueError):
        _context(expires_at=datetime(2026, 8, 19, 12, 0))


def test_pending_outbox_contains_only_metadata_values(tmp_path: Path) -> None:
    repository, database_path = _setup(tmp_path)

    repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM memory_index_outbox").fetchone()
    serialized_values = " ".join("" if value is None else str(value) for value in row)
    forbidden_values = (
        PRIVATE_MARKER,
        PRIVATE_SOURCE_HASH,
        "embedding",
        "source_hash",
        "finding",
        "parser_frame",
    )
    assert all(value not in serialized_values for value in forbidden_values)


def test_outbox_exception_and_logs_do_not_expose_content_or_privacy_artifacts(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    repository, database_path = _setup(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_upsert BEFORE INSERT ON memory_index_outbox "
            "BEGIN SELECT RAISE(ABORT, 'metadata-only failure'); END"
        )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(sqlite3.IntegrityError) as captured:
        repository.save(
            character_id="miori", candidate=_candidate(), context=_context()
        )

    observed_log_fields = [record.getMessage() for record in caplog.records]
    forbidden_values = (
        PRIVATE_MARKER,
        PRIVATE_SOURCE_HASH,
        "embedding",
        "source_hash",
        "finding",
        "parser_frame",
    )
    assert all(value not in str(captured.value) for value in forbidden_values)
    assert all(
        value not in message
        for message in observed_log_fields
        for value in forbidden_values
    )

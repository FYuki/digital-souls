import sqlite3
from pathlib import Path

import pytest


EXPECTED_TABLES = {
    "approved_memories",
    "memory_sources",
    "memory_lineage",
    "memory_write_receipts",
    "memory_index_outbox",
    "temporary_provider_records",
}
MEMORY_ONE = "00000000-0000-4000-8000-000000000001"
MEMORY_TWO = "00000000-0000-4000-8000-000000000002"
OUTBOX_ONE = "10000000-0000-4000-8000-000000000001"


def _initialize(tmp_path: Path):
    from app.memory.persistence.schema import initialize_persona_memory_schema
    from app.runtime_paths import resolve_runtime_paths

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    paths = resolve_runtime_paths(
        {
            "DS_ENVIRONMENT_ID": "test",
            "DS_DATA_DIR": str(tmp_path / "test-data"),
        },
        repository_root,
    )
    initialize_persona_memory_schema(paths, repository_root)
    return paths


def _approved_values(
    *,
    memory_id: str = MEMORY_ONE,
    character_id: str = "miori",
    memory_kind: str = "SEMANTIC",
    memory_type: str = "USER_PREFERENCE",
    episodic_event_type: str | None = None,
    provider_id: str = "core",
    formation_method: str = "EXTRACTED",
    status: str = "ACTIVE",
    idempotency_key: str = "conversation-1:turn-1:0:extractor-v1",
) -> tuple[object, ...]:
    timestamp = "2026-08-18T00:00:00.000000Z"
    return (
        memory_id,
        character_id,
        provider_id,
        memory_kind,
        memory_type,
        episodic_event_type,
        formation_method,
        1,
        "ユーザーは短い回答を好む",
        '{"object":"短い回答","polarity":"LIKE"}',
        "policy-v1",
        "classifier-v1",
        "gemma4:e4b",
        "model-digest",
        "prompt-v1",
        1,
        status,
        idempotency_key,
        timestamp,
        "Asia/Tokyo",
        "SECOND",
        None,
        timestamp,
        None,
        timestamp,
        timestamp,
    )


APPROVED_COLUMNS = (
    "id, character_id, provider_id, memory_kind, memory_type, "
    "episodic_event_type, formation_method, schema_version, normalized_text, "
    "structured_value, policy_version, classifier_version, model_id, "
    "model_digest, prompt_version, content_version, status, idempotency_key, "
    "effective_at, effective_timezone, temporal_precision, expires_at, "
    "last_user_mentioned_at, last_consolidated_at, created_at, updated_at"
)


def _insert_approved(connection: sqlite3.Connection, values: tuple[object, ...]) -> None:
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO approved_memories ({APPROVED_COLUMNS}) VALUES ({placeholders})",
        values,
    )


def test_schema_creates_only_the_persona_memory_tables(tmp_path: Path) -> None:
    paths = _initialize(tmp_path)

    with sqlite3.connect(paths.persona_memory_sqlite_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    assert tables == EXPECTED_TABLES
    assert paths.persona_memory_sqlite_path != paths.sqlite_path
    assert not paths.sqlite_path.exists()


@pytest.mark.parametrize(
    ("overrides"),
    [
        {"memory_kind": "PROCEDURAL"},
        {"memory_type": "GENERAL_MEMORY"},
        {"provider_id": "temporary:recipe"},
        {"formation_method": "INFERRED"},
        {"status": "DELETED"},
    ],
)
def test_approved_memory_rejects_values_outside_the_allowlists(
    overrides: dict[str, str], tmp_path: Path
) -> None:
    paths = _initialize(tmp_path)

    with sqlite3.connect(paths.persona_memory_sqlite_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_approved(connection, _approved_values(**overrides))


@pytest.mark.parametrize(
    ("memory_kind", "memory_type", "episodic_event_type"),
    [
        ("SEMANTIC", "EPISODIC_EVENT", "SHARED_MILESTONE"),
        ("SEMANTIC", "EPISODIC_EVENT", "ACHIEVEMENT"),
        ("SEMANTIC", "EPISODIC_EVENT", "DECISION"),
        ("SEMANTIC", "EPISODIC_EVENT", "OUTCOME"),
        ("SEMANTIC", "EPISODIC_EVENT", "CHANGE"),
        ("EPISODIC", "EPISODIC_EVENT", None),
        ("EPISODIC", "USER_PREFERENCE", None),
        ("SEMANTIC", "USER_PREFERENCE", "DECISION"),
        ("EPISODIC", "INTERACTION_PREFERENCE", None),
        ("SEMANTIC", "INTERACTION_PREFERENCE", "DECISION"),
    ],
)
def test_approved_memory_rejects_allowlisted_values_that_violate_cross_field_constraint(
    memory_kind: str,
    memory_type: str,
    episodic_event_type: str | None,
    tmp_path: Path,
) -> None:
    paths = _initialize(tmp_path)

    with sqlite3.connect(paths.persona_memory_sqlite_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_approved(
                connection,
                _approved_values(
                    memory_kind=memory_kind,
                    memory_type=memory_type,
                    episodic_event_type=episodic_event_type,
                ),
            )


def test_approved_memory_requires_character_and_character_scoped_idempotency_key(
    tmp_path: Path,
) -> None:
    paths = _initialize(tmp_path)

    with sqlite3.connect(paths.persona_memory_sqlite_path) as connection:
        _insert_approved(connection, _approved_values())
        with pytest.raises(sqlite3.IntegrityError):
            _insert_approved(
                connection,
                _approved_values(memory_id=MEMORY_TWO),
            )
        _insert_approved(
            connection,
            _approved_values(
                memory_id=MEMORY_TWO,
                character_id="other",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_approved(
                connection,
                _approved_values(
                    memory_id="00000000-0000-4000-8000-000000000003",
                    character_id=None,  # type: ignore[arg-type]
                    idempotency_key="different-key",
                ),
            )


def test_schema_creates_worker_and_active_memory_indexes(tmp_path: Path) -> None:
    paths = _initialize(tmp_path)

    with sqlite3.connect(paths.persona_memory_sqlite_path) as connection:
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    assert "idx_memory_index_outbox_pending" in indexes
    assert "idx_approved_memories_active" in indexes


def test_sources_and_lineage_enforce_memory_foreign_keys_and_relation_allowlist(
    tmp_path: Path,
) -> None:
    paths = _initialize(tmp_path)

    with sqlite3.connect(paths.persona_memory_sqlite_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_approved(connection, _approved_values())
        _insert_approved(
            connection,
            _approved_values(
                memory_id=MEMORY_TWO,
                idempotency_key="conversation-1:turn-1:1:extractor-v1",
            ),
        )
        connection.execute(
            "INSERT INTO memory_sources "
            "(character_id, memory_id, source_type, source_provider_id, source_ref) "
            "VALUES (?, ?, ?, ?, ?)",
            ("miori", MEMORY_ONE, "CONVERSATION_TURN", "core", "c1:t1"),
        )
        connection.execute(
            "INSERT INTO memory_sources "
            "(character_id, memory_id, source_type, source_provider_id, source_ref) "
            "VALUES (?, ?, ?, ?, ?)",
            ("miori", MEMORY_TWO, "CONVERSATION_TURN", "core", "c1:t1"),
        )
        connection.execute(
            "INSERT INTO memory_sources "
            "(character_id, memory_id, source_type, source_provider_id, source_ref) "
            "VALUES (?, ?, ?, ?, ?)",
            ("miori", MEMORY_ONE, "ADDON_EVENT", "temporary:recipe", "event-1"),
        )
        connection.execute(
            "INSERT INTO memory_lineage "
            "(character_id, memory_id, related_memory_id, relation) "
            "VALUES (?, ?, ?, ?)",
            ("miori", MEMORY_TWO, MEMORY_ONE, "SUPERSEDES"),
        )
        source_count = connection.execute(
            "SELECT COUNT(*) FROM memory_sources WHERE source_ref = ?",
            ("c1:t1",),
        ).fetchone()[0]
        lineage_count = connection.execute(
            "SELECT COUNT(*) FROM memory_lineage WHERE relation = ?",
            ("SUPERSEDES",),
        ).fetchone()[0]
        source_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(memory_sources)")
        }

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO memory_sources "
                "(character_id, memory_id, source_type, source_provider_id, source_ref) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "miori",
                    "00000000-0000-4000-8000-000000000099",
                    "CONVERSATION_TURN",
                    "core",
                    "c1:t2",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO memory_sources "
                "(character_id, memory_id, source_type, source_provider_id, source_ref) "
                "VALUES (?, ?, ?, ?, ?)",
                ("miori", MEMORY_ONE, "RAW_BODY", "core", "source-2"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO memory_lineage "
                "(character_id, memory_id, related_memory_id, relation) "
                "VALUES (?, ?, ?, ?)",
                ("miori", MEMORY_TWO, MEMORY_ONE, "PARENT_OF"),
            )

    assert source_count == 2
    assert lineage_count == 1
    assert source_columns.isdisjoint({"body", "content", "normalized_text"})


def test_outbox_is_metadata_only_and_survives_memory_hard_delete(
    tmp_path: Path,
) -> None:
    paths = _initialize(tmp_path)

    with sqlite3.connect(paths.persona_memory_sqlite_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_approved(connection, _approved_values())
        connection.execute(
            "INSERT INTO memory_index_outbox "
            "(id, memory_id, character_id, operation, status, attempt_count, "
            "last_error_code, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                OUTBOX_ONE,
                MEMORY_ONE,
                "miori",
                "DELETE",
                "PENDING",
                0,
                None,
                "2026-08-18T00:00:00.000000Z",
                "2026-08-18T00:00:00.000000Z",
            ),
        )
        connection.execute(
            "DELETE FROM approved_memories WHERE character_id = ? AND id = ?",
            ("miori", MEMORY_ONE),
        )
        outbox_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(memory_index_outbox)")
        }
        remaining = connection.execute(
            "SELECT operation, status FROM memory_index_outbox WHERE id = ?",
            (OUTBOX_ONE,),
        ).fetchone()

    forbidden_columns = {
        "normalized_text",
        "structured_value",
        "embedding",
        "source_hash",
        "finding",
        "parser_frame",
    }
    assert outbox_columns.isdisjoint(forbidden_columns)
    assert remaining == ("DELETE", "PENDING")


@pytest.mark.parametrize(
    ("operation", "status", "attempt_count"),
    [
        ("REINDEX", "PENDING", 0),
        ("UPSERT", "RETRYING", 0),
        ("UPSERT", "PENDING", -1),
    ],
)
def test_outbox_rejects_values_outside_its_constraints(
    operation: str,
    status: str,
    attempt_count: int,
    tmp_path: Path,
) -> None:
    paths = _initialize(tmp_path)

    with sqlite3.connect(paths.persona_memory_sqlite_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO memory_index_outbox "
                "(id, memory_id, character_id, operation, status, attempt_count, "
                "last_error_code, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    OUTBOX_ONE,
                    MEMORY_ONE,
                    "miori",
                    operation,
                    status,
                    attempt_count,
                    None,
                    "2026-08-18T00:00:00.000000Z",
                    "2026-08-18T00:00:00.000000Z",
                ),
            )


def test_temporary_provider_records_reject_duplicate_source_identity(
    tmp_path: Path,
) -> None:
    paths = _initialize(tmp_path)
    values = (
        "20000000-0000-4000-8000-000000000001",
        "miori",
        "temporary:recipe",
        "recipe-1",
        "RECIPE",
        '{"name":"カレー"}',
        "2026-08-18T00:00:00.000000Z",
        "2026-08-18T00:00:00.000000Z",
        "2026-08-18T00:00:00.000000Z",
    )

    with sqlite3.connect(paths.persona_memory_sqlite_path) as connection:
        connection.execute(
            "INSERT INTO temporary_provider_records "
            "(id, character_id, provider_id, source_ref, record_type, "
            "structured_value, effective_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        duplicate_values = (
            "20000000-0000-4000-8000-000000000002",
            *values[1:],
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO temporary_provider_records "
                "(id, character_id, provider_id, source_ref, record_type, "
                "structured_value, effective_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                duplicate_values,
            )

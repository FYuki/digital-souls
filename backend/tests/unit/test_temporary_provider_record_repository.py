import sqlite3
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from uuid import UUID

import pytest


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _repository(tmp_path: Path):
    from app.memory.persistence.schema import initialize_persona_memory_schema
    from app.memory.persistence.temporary_repository import (
        TemporaryProviderRecordRepository,
    )
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
    repository = TemporaryProviderRecordRepository(
        database_path=paths.persona_memory_sqlite_path,
        clock=lambda: NOW,
        uuid_factory=lambda: UUID(
            f"00000000-0000-4000-8000-{next(identifiers):012d}"
        ),
    )
    return repository, paths.persona_memory_sqlite_path


@pytest.mark.parametrize(
    "provider_id", ["temporary:agriculture", "temporary:recipe"]
)
def test_temporary_repository_accepts_only_the_two_temporary_providers(
    provider_id: str, tmp_path: Path
) -> None:
    from app.memory.persistence.contracts import TemporaryProviderRecordInput

    repository, _database_path = _repository(tmp_path)

    saved = repository.save(
        character_id="miori",
        record=TemporaryProviderRecordInput(
            provider_id=provider_id,
            source_ref="source-1",
            record_type="ENTRY",
            structured_value='{"value":"synthetic"}',
            effective_at=NOW,
        ),
    )

    assert saved.character_id == "miori"
    assert saved.provider_id == provider_id


def test_temporary_repository_rejects_core_without_creating_any_record(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import TemporaryProviderRecordInput

    repository, database_path = _repository(tmp_path)

    with pytest.raises(ValueError):
        repository.save(
            character_id="miori",
            record=TemporaryProviderRecordInput(
                provider_id="core",
                source_ref="source-1",
                record_type="ENTRY",
                structured_value='{"value":"synthetic"}',
                effective_at=NOW,
            ),
        )

    with sqlite3.connect(database_path) as connection:
        count_rows = connection.execute(
            "SELECT COUNT(*) FROM temporary_provider_records"
        ).fetchone()[0]
    assert count_rows == 0


def test_temporary_save_and_delete_never_create_persona_outbox_rows(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import TemporaryProviderRecordInput

    repository, database_path = _repository(tmp_path)
    saved = repository.save(
        character_id="miori",
        record=TemporaryProviderRecordInput(
            provider_id="temporary:recipe",
            source_ref="recipe-1",
            record_type="ENTRY",
            structured_value='{"value":"synthetic"}',
            effective_at=NOW,
        ),
    )

    repository.hard_delete_after_migration(
        character_id="miori",
        provider_id="temporary:recipe",
        record_id=saved.id,
    )

    with sqlite3.connect(database_path) as connection:
        temporary_count = connection.execute(
            "SELECT COUNT(*) FROM temporary_provider_records"
        ).fetchone()[0]
        approved_count = connection.execute(
            "SELECT COUNT(*) FROM approved_memories"
        ).fetchone()[0]
        outbox_count = connection.execute(
            "SELECT COUNT(*) FROM memory_index_outbox"
        ).fetchone()[0]
    assert temporary_count == 0
    assert approved_count == 0
    assert outbox_count == 0


def test_temporary_delete_requires_character_and_provider_boundaries(
    tmp_path: Path,
) -> None:
    from app.memory.persistence.contracts import TemporaryProviderRecordInput

    repository, database_path = _repository(tmp_path)
    saved = repository.save(
        character_id="miori",
        record=TemporaryProviderRecordInput(
            provider_id="temporary:agriculture",
            source_ref="harvest-1",
            record_type="ENTRY",
            structured_value='{"value":"synthetic"}',
            effective_at=NOW,
        ),
    )

    operations = (
        lambda: repository.hard_delete_after_migration(
            character_id="other",
            provider_id="temporary:agriculture",
            record_id=saved.id,
        ),
        lambda: repository.hard_delete_after_migration(
            character_id="miori",
            provider_id="temporary:recipe",
            record_id=saved.id,
        ),
    )
    for operation in operations:
        try:
            operation()
        except Exception:
            continue

    with sqlite3.connect(database_path) as connection:
        remaining = connection.execute(
            "SELECT character_id, provider_id FROM temporary_provider_records"
        ).fetchone()
    assert remaining == ("miori", "temporary:agriculture")
